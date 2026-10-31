"""Open Alpha Link sequence manifests: parsing and matching photos to frames.

The iOS app writes one manifest per multi-shot run (focus stack, bracket,
intervalometer, burst, bulb). The contract, including the matching
algorithm implemented here, is docs/SEQUENCE_MANIFEST.md.
"""

import json
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

MANIFEST_FORMAT = "open-alpha-link.sequence"
MANIFEST_VERSION = 1
FRAME_TOLERANCE_S = 1.5
ACCEPT_RATIO = 0.8

KIND_TAGS = {
    "focus_stack": "focus_stack",
    "exposure_bracket": "exposure_bracket",
    "intervalometer": "timelapse",
    "burst": "burst",
    "bulb": "bulb",
}

JPEG_SUFFIXES = {".jpg", ".jpeg"}


@dataclass(frozen=True)
class Frame:
    """One shutter press, in the photo's local wall-clock time."""

    index: int
    pressed_at: datetime
    released_at: datetime


@dataclass(frozen=True)
class Annotations:
    """What the user typed on the phone. Every field is optional."""

    title: Optional[str] = None
    notes: Optional[str] = None
    subject: Optional[str] = None
    tags: Tuple[str, ...] = ()
    album: Optional[str] = None
    lens: Optional[str] = None


def parse_annotations(data: Any) -> Annotations:
    if not isinstance(data, dict):
        return Annotations()

    def text(key: str) -> Optional[str]:
        value = data.get(key)
        return str(value).strip() or None if value is not None else None

    tags = data.get("tags") or []
    return Annotations(
        title=text("title"),
        notes=text("notes"),
        subject=text("subject"),
        tags=tuple(t for t in (str(tag).strip().lower() for tag in tags if tag) if t)
        if isinstance(tags, list)
        else (),
        album=text("album"),
        lens=text("lens"),
    )


def _plan_value(key: str, value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return key.replace("_", " ") if value else None
    if isinstance(value, str):
        return value or None
    for suffix, unit in (("_ms", "ms"), ("_s", "s")):
        if key.endswith(suffix):
            return f"{key[: -len(suffix)].replace('_', ' ')} {value} {unit}"
    return f"{key.replace('_', ' ')} {value}"


@dataclass(frozen=True)
class Manifest:
    id: str
    kind: str
    outcome: str
    time_zone: str
    started_at: datetime  # local wall-clock, naive
    ended_at: datetime  # local wall-clock, naive
    camera_model: Optional[str]
    expected_images: Optional[int]
    frames: Tuple[Frame, ...]
    plan: Tuple[Tuple[str, Any], ...] = ()
    annotations: Annotations = Annotations()

    @property
    def short_id(self) -> str:
        return self.id[:8].lower()

    @property
    def tags(self) -> List[str]:
        """Keywords every member of this sequence gets."""
        return [KIND_TAGS.get(self.kind, self.kind), f"stack_{self.short_id}"]

    def plan_summary(self) -> str:
        """'in, step 16, nudge 100 ms, settle 1500 ms' (frame count left out)."""
        parts = [_plan_value(k, v) for k, v in self.plan if k != "frames"]
        return ", ".join(p for p in parts if p)

    def user_comment(self, frame_index: int, position: int, exposures: int) -> str:
        """'focus_stack 7/24, in, step 16, ...' for a member's UserComment.

        A single press that wrote several images (a held bracket or burst)
        counts images instead of presses.
        """
        if len(self.frames) == 1 and exposures > 1:
            where = f"{position}/{exposures}"
        else:
            where = f"{frame_index}/{len(self.frames)}"
        plan = self.plan_summary()
        return f"{self.kind} {where}" + (f", {plan}" if plan else "")


@dataclass(frozen=True)
class Shot:
    """A photo's capture time and camera, from EXIF."""

    path: str
    taken_at: datetime  # camera wall-clock, naive
    model: Optional[str]


@dataclass(frozen=True)
class MatchResult:
    offset_seconds: float
    matched_frames: int
    total_frames: int
    assignments: Dict[str, int]  # photo path -> frame index

    @property
    def accepted(self) -> bool:
        return (
            self.total_frames > 0
            and self.matched_frames >= ACCEPT_RATIO * self.total_frames
        )


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_manifest(data: Any) -> Optional[Manifest]:
    """Manifest from decoded JSON; None for anything that isn't a v1 manifest."""
    if not isinstance(data, dict) or data.get("format") != MANIFEST_FORMAT:
        return None
    if data.get("version") != MANIFEST_VERSION:
        return None
    try:
        zone = ZoneInfo(data["time_zone"])

        def local(value: str) -> datetime:
            return _utc(value).astimezone(zone).replace(tzinfo=None)

        frames = tuple(
            Frame(
                index=int(f["index"]),
                pressed_at=local(f["pressed_at"]),
                released_at=local(f.get("released_at") or f["pressed_at"]),
            )
            for f in data.get("frames") or []
        )
        camera = data.get("camera") or {}
        plan = data.get("plan")
        expected = data.get("expected_images")
        return Manifest(
            id=str(data["id"]),
            kind=str(data["kind"]),
            outcome=str(data.get("outcome") or ""),
            time_zone=data["time_zone"],
            started_at=local(data["started_at"]),
            ended_at=local(data["ended_at"]),
            camera_model=camera.get("model") or None,
            expected_images=int(expected) if expected is not None else None,
            frames=tuple(sorted(frames, key=lambda f: f.index)),
            plan=tuple(plan.items()) if isinstance(plan, dict) else (),
            annotations=parse_annotations(data.get("annotations")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def load_manifest(path: Path) -> Optional[Manifest]:
    """Read a manifest file; None for unreadable or foreign JSON, never raises."""
    try:
        return parse_manifest(json.loads(path.read_text()))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def find_manifest_files(directories: Sequence[Path]) -> List[Path]:
    """Every *.json under the given directories (manifests or not)."""
    found: List[Path] = []
    for directory in directories:
        try:
            if directory.is_dir():
                found.extend(sorted(directory.rglob("*.json")))
        except OSError:
            continue
    return found


class _FrameIndex:
    """Frames that can lie within a time range, found by bisection."""

    def __init__(self, frames: Sequence[Frame]):
        self.frames = sorted(frames, key=lambda f: f.pressed_at)
        self.pressed = [f.pressed_at for f in self.frames]
        # Running max keeps the list sorted even if a long press overlaps the next
        self.released_max: List[datetime] = []
        for frame in self.frames:
            latest = self.released_max[-1] if self.released_max else frame.released_at
            self.released_max.append(max(latest, frame.released_at))

    def near(self, start: datetime, end: datetime) -> List[Frame]:
        """Frames whose [pressed, released] could intersect [start, end]."""
        low = bisect_left(self.released_max, start)
        high = bisect_right(self.pressed, end)
        return [f for f in self.frames[low:high] if f.released_at >= start]


def _offset(shot: Shot, moment: datetime) -> float:
    return (moment - shot.taken_at).total_seconds()


def _candidates(
    manifest: Manifest, shots: Sequence[Shot], max_skew: float
) -> List[Shot]:
    skew = timedelta(seconds=max_skew)
    low, high = manifest.started_at - skew, manifest.ended_at + skew
    return [
        s
        for s in shots
        if (manifest.camera_model is None or s.model == manifest.camera_model)
        and low <= s.taken_at <= high
    ]


def _frame_for(shot: Shot, index: _FrameIndex, offset: float) -> Optional[int]:
    """Index of the frame whose window holds the shifted photo time, nearest first."""
    shifted = shot.taken_at + timedelta(seconds=offset)
    tolerance = timedelta(seconds=FRAME_TOLERANCE_S)
    best: Optional[Tuple[float, int]] = None
    for frame in index.near(shifted - tolerance, shifted + tolerance):
        start = frame.pressed_at - timedelta(seconds=FRAME_TOLERANCE_S)
        end = frame.released_at + timedelta(seconds=FRAME_TOLERANCE_S)
        if not (start <= shifted <= end):
            continue
        if frame.pressed_at <= shifted <= frame.released_at:
            distance = 0.0
        else:
            distance = min(
                abs((shifted - frame.pressed_at).total_seconds()),
                abs((shifted - frame.released_at).total_seconds()),
            )
        if best is None or distance < best[0]:
            best = (distance, frame.index)
    return best[1] if best else None


def _best_regions(
    manifest: Manifest, shots: Sequence[Shot], max_skew: float
) -> Tuple[int, List[Tuple[float, float]]]:
    """Most frames any offset reaches, and the offset ranges that reach it.

    Each (photo, frame) pair matches over one closed interval of offsets, so
    the count only changes at interval endpoints: a sweep over those
    endpoints finds every optimal range exactly, with no grid search.
    """
    index = _FrameIndex(manifest.frames)
    reach = timedelta(seconds=max_skew + FRAME_TOLERANCE_S)
    events: List[Tuple[float, int, int]] = []  # (offset, 0=start/1=end, frame)
    for shot in _candidates(manifest, shots, max_skew):
        for frame in index.near(shot.taken_at - reach, shot.taken_at + reach):
            low = max(_offset(shot, frame.pressed_at) - FRAME_TOLERANCE_S, -max_skew)
            high = min(_offset(shot, frame.released_at) + FRAME_TOLERANCE_S, max_skew)
            if low <= high:
                events.append((low, 0, frame.index))
                events.append((high, 1, frame.index))
    if not events:
        return 0, []

    events.sort()
    points = sorted({e[0] for e in events})
    active: Dict[int, int] = {}
    matched = 0
    best = 0
    regions: List[Tuple[float, float]] = []
    span_start: Optional[float] = None
    i = 0
    for n, point in enumerate(points):
        # Closed intervals: open everything starting here, evaluate the point,
        # close everything ending here, then evaluate the gap to the next point.
        while i < len(events) and events[i][0] == point and events[i][1] == 0:
            frame = events[i][2]
            active[frame] = active.get(frame, 0) + 1
            matched += active[frame] == 1
            i += 1
        if matched > best:
            best, regions, span_start = matched, [], point
        elif matched == best and span_start is None:
            span_start = point
        while i < len(events) and events[i][0] == point:
            frame = events[i][2]
            active[frame] -= 1
            matched -= active[frame] == 0
            i += 1
        if span_start is not None and (matched < best or n == len(points) - 1):
            regions.append((span_start, point))
            span_start = None
    return best, regions


def _assign(manifest: Manifest, shots: Sequence[Shot], offset: float) -> Dict[str, int]:
    index = _FrameIndex(manifest.frames)
    assignments: Dict[str, int] = {}
    for shot in shots:
        frame = _frame_for(shot, index, offset)
        if frame is not None:
            assignments[shot.path] = frame
    return assignments


def match_sequence(
    manifest: Manifest, shots: Sequence[Shot], max_skew: float
) -> MatchResult:
    """Best clock offset and the photo -> frame assignment it produces.

    Within the offset ranges that reach the most frames, the probe whose
    one-frame-per-photo assignment keeps the most frames wins, then the one
    that places the most photos (a held press gains all its images), then
    the range closest to zero. The reported offset is the middle of the
    range, since whole-second EXIF times leave every offset in it equally
    likely.
    """
    candidates = _candidates(manifest, shots, max_skew)
    best_count, regions = _best_regions(manifest, candidates, max_skew)
    best: Tuple[Tuple[int, int, float], float, Dict[str, int]] = ((0, 0, 0.0), 0.0, {})
    if best_count:
        for low, high in regions:
            middle = (low + high) / 2
            for probe in (middle, low, high):
                assignments = _assign(manifest, candidates, probe)
                score = (
                    len(set(assignments.values())),
                    len(assignments),
                    -abs(middle),
                )
                if score > best[0]:
                    best = (score, probe, assignments)
    _, offset, assignments = best
    return MatchResult(
        offset_seconds=round(offset, 3),
        matched_frames=len(set(assignments.values())),
        total_frames=len(manifest.frames),
        assignments=assignments,
    )


def order_members(assignments: Dict[str, int], shots: Sequence[Shot]) -> List[str]:
    """Member paths in shooting order: frame, then capture time, then name."""
    taken = {s.path: s.taken_at for s in shots}
    return sorted(
        assignments,
        key=lambda p: (assignments[p], taken.get(p, datetime.min), Path(p).name),
    )


def _pair_key(path: str) -> Tuple[str, str]:
    p = Path(path)
    return str(p.parent), p.stem.lower()


def group_pairs(ordered_paths: Sequence[str]) -> List[List[str]]:
    """Ordered member paths grouped so a RAW+JPEG pair is one exposure."""
    groups: Dict[Tuple[str, str], List[str]] = {}
    for path in ordered_paths:
        groups.setdefault(_pair_key(path), []).append(path)
    return list(groups.values())


def pick_representative(ordered_paths: Sequence[str]) -> Optional[str]:
    """The middle exposure, as its JPEG when it was shot RAW+JPEG."""
    exposures = group_pairs(ordered_paths)
    if not exposures:
        return None
    middle = exposures[(len(exposures) - 1) // 2]
    jpegs = [p for p in middle if Path(p).suffix.lower() in JPEG_SUFFIXES]
    return (jpegs or middle)[0]


def parse_exif_time(
    value: Optional[str], subsec: Optional[Any] = None
) -> Optional[datetime]:
    """exiftool 'YYYY:MM:DD HH:MM:SS' (+ SubSecTimeOriginal digits) -> datetime."""
    if not value:
        return None
    text = str(value).strip()[:19]
    try:
        parsed = datetime.strptime(text, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    digits = "".join(ch for ch in str(subsec or "") if ch.isdigit())
    if digits:
        parsed += timedelta(seconds=float(f"0.{digits}"))
    return parsed
