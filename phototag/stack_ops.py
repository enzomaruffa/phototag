"""Stack workflows over the state DB: import manifests and match inbox photos.

The matching rules live in phototag.stacks; this module feeds them EXIF
data and persists the results.
"""

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import focus_merge
from .media import RAW_EXTENSIONS, find_media_files, unique_destination
from .stacks import (
    JPEG_SUFFIXES,
    KIND_TAGS,
    Annotations,
    Manifest,
    Shot,
    find_manifest_files,
    group_pairs,
    load_manifest,
    match_sequence,
    order_members,
    parse_exif_time,
    parse_manifest,
    pick_representative,
)
from .storage.exif import EXIFHandler
from .storage.immich import ImmichUploader
from .storage.state_db import PhotoStatus, ProcessingStateDB

logger = logging.getLogger(__name__)

# Where `phototag receive` writes manifests pushed by the app, relative to cwd like the DB
RECEIVED_MANIFEST_DIR = Path(".phototag/manifests")
DEFAULT_MAX_SKEW_S = 120.0

# Member statuses whose files already carry their EXIF; annotations edited
# later are not rewritten into them
WRITTEN_STATUSES = {
    PhotoStatus.EXIF_WRITTEN.value,
    PhotoStatus.MOVING.value,
    PhotoStatus.PROCESSED.value,
}


def manifest_dirs() -> List[Path]:
    """The received-manifests dir, then SEQUENCE_MANIFEST_DIR (comma list allowed)."""
    raw = os.getenv("SEQUENCE_MANIFEST_DIR") or ""
    extra = [Path(d.strip()).expanduser() for d in raw.split(",") if d.strip()]
    return [RECEIVED_MANIFEST_DIR, *extra]


def max_clock_skew() -> float:
    try:
        return float(os.getenv("STACK_MAX_CLOCK_SKEW_SECONDS") or DEFAULT_MAX_SKEW_S)
    except ValueError:
        logger.warning("STACK_MAX_CLOCK_SKEW_SECONDS is not a number, using 120")
        return DEFAULT_MAX_SKEW_S


@dataclass
class ImportSummary:
    imported: int = 0
    already_known: int = 0
    updated: List[str] = field(default_factory=list)
    # Annotations changed after some members were already written; those
    # files keep the old annotations
    edited_after_processing: List[str] = field(default_factory=list)
    matched: List[str] = field(default_factory=list)
    unmatched: List[str] = field(default_factory=list)


def import_manifests(
    state_db: ProcessingStateDB, directories: List[Path]
) -> ImportSummary:
    """Store every manifest found.

    Re-importing a known id only picks up edited annotations: the app
    rewrites a manifest when the user edits them, and the frames never change.
    """
    summary = ImportSummary()
    for path in find_manifest_files(directories):
        manifest = load_manifest(path)
        if manifest is None:
            continue  # some other JSON file
        text = path.read_text()
        if state_db.import_sequence(
            manifest.id,
            manifest.kind,
            text,
            str(path),
            manifest.started_at.isoformat(),
            len(manifest.frames),
        ):
            summary.imported += 1
            continue

        summary.already_known += 1
        stored = state_db.get_sequence(manifest.id)
        if stored is None:
            continue
        current = json.loads(stored["manifest_json"])
        incoming = json.loads(text).get("annotations")
        if current.get("annotations") == incoming:
            continue
        current["annotations"] = incoming
        state_db.update_sequence_manifest(manifest.id, json.dumps(current), str(path))
        summary.updated.append(manifest.id)
        if any(
            m["status"] in WRITTEN_STATUSES
            for m in state_db.get_stack_members(manifest.id)
        ):
            summary.edited_after_processing.append(manifest.id)
    return summary


def read_shots(paths: List[Path], exif: EXIFHandler) -> List[Shot]:
    """Capture time and camera model of each photo, from one exiftool call."""
    shots = []
    for record in exif.read_shot_metadata(paths):
        taken_at = parse_exif_time(
            record.get("DateTimeOriginal"), record.get("SubSecTimeOriginal")
        )
        if taken_at is None:
            continue
        model = record.get("Model")
        shots.append(
            Shot(str(record["SourceFile"]), taken_at, str(model) if model else None)
        )
    return shots


def match_inbox(
    state_db: ProcessingStateDB,
    inbox_dir: Path,
    max_skew: float,
    exif: Optional[EXIFHandler] = None,
    summary: Optional[ImportSummary] = None,
) -> ImportSummary:
    """Match every unmatched sequence against the photos in the inbox.

    A photo joins at most one sequence: earlier sequences claim first, and
    photos already in a stack are left out.
    """
    summary = summary or ImportSummary()
    pending: List[Manifest] = []
    for row in state_db.get_sequences(status="unmatched"):
        manifest = parse_manifest(json.loads(row["manifest_json"]))
        if manifest is not None:
            pending.append(manifest)
    if not pending:
        return summary

    # Merged stack images carry their middle frame's capture time and camera
    taken = state_db.get_stack_member_paths() | state_db.get_merged_inbox_paths()
    photos = [
        p
        for p in find_media_files(inbox_dir, include_videos=False)
        if str(p) not in taken
    ]
    shots = read_shots(photos, exif or EXIFHandler())

    for manifest in sorted(pending, key=lambda m: m.started_at):
        result = match_sequence(manifest, shots, max_skew)
        if not result.accepted:
            state_db.record_sequence_attempt(
                manifest.id, result.offset_seconds, result.matched_frames
            )
            summary.unmatched.append(manifest.id)
            continue

        ordered = order_members(result.assignments, shots)
        representative = pick_representative(ordered)
        positions = _positions(ordered)
        state_db.record_sequence_match(
            manifest.id,
            result.offset_seconds,
            result.matched_frames,
            [
                (
                    path,
                    result.assignments[path],
                    positions[path],
                    int(path == representative),
                )
                for path in ordered
            ],
        )
        summary.matched.append(manifest.id)
        shots = [s for s in shots if s.path not in result.assignments]
    return summary


def _positions(ordered: List[str]) -> Dict[str, int]:
    """1-based exposure number per path; a RAW+JPEG pair shares one number."""
    positions = {}
    for number, group in enumerate(group_pairs(ordered), start=1):
        for path in group:
            positions[path] = number
    return positions


@dataclass(frozen=True)
class MemberContext:
    """What processing needs to know about a photo that belongs to a stack."""

    manifest: Manifest
    frame_index: int
    position: int
    exposures: int
    is_representative: bool
    representative_analysis: Optional[str]  # saved ai_response_json

    @property
    def user_comment(self) -> str:
        return self.manifest.user_comment(
            self.frame_index, self.position, self.exposures
        )


def member_context(
    state_db: ProcessingStateDB, filepath: str
) -> Optional[MemberContext]:
    row = state_db.get_stack_membership(filepath)
    if not row:
        return None
    manifest = parse_manifest(json.loads(row["manifest_json"]))
    if manifest is None:
        return None
    return MemberContext(
        manifest=manifest,
        frame_index=row["frame_index"],
        position=row["position"],
        exposures=row["exposures"] or 1,
        is_representative=bool(row["is_representative"]),
        representative_analysis=row["representative_ai_response_json"],
    )


def merged_image_analysis(state_db: ProcessingStateDB, filepath: str) -> Optional[str]:
    """For a merged stack image in the inbox, its representative's saved analysis."""
    row = state_db.get_merged_sequence(filepath)
    return row["representative_ai_response_json"] if row else None


def member_keywords(stack: Optional[MemberContext]) -> List[str]:
    """Keywords a stack member gets on top of its own analysis."""
    if not stack:
        return []
    return stack.manifest.tags + list(stack.manifest.annotations.tags)


def member_description(description: str, stack: Optional[MemberContext]) -> str:
    """AI description with the user's subject kept and their notes appended."""
    if not stack:
        return description
    return annotated_description(description, stack.manifest.annotations)


def annotated_description(description: str, notes: Annotations) -> str:
    parts = [description.strip()]
    if notes.subject and notes.subject.lower() not in description.lower():
        parts.append(f"Subject: {notes.subject}.")
    if notes.notes:
        parts.append(notes.notes)
    return " ".join(p for p in parts if p)


# A member in one of these hasn't reached Immich yet, so its stack waits
IN_FLIGHT_STATUSES = {
    PhotoStatus.PENDING.value,
    PhotoStatus.LOCKED.value,
    PhotoStatus.AI_ANALYZING.value,
    PhotoStatus.AI_ANALYZED.value,
    PhotoStatus.AWAITING_TAG_REVIEW.value,
    PhotoStatus.EXIF_WRITING.value,
    PhotoStatus.EXIF_WRITTEN.value,
    PhotoStatus.MOVING.value,
}


@dataclass
class ImmichStackSummary:
    stacked: int = 0
    albums: int = 0
    waiting: int = 0
    errors: List[str] = field(default_factory=list)


def link_immich_stacks(
    state_db: ProcessingStateDB,
    uploader: ImmichUploader,
    api_key: str,
    album_override: Optional[str] = None,
) -> ImmichStackSummary:
    """Stack each fully uploaded sequence in Immich and file it in its album.

    Assets are found by the SHA-1 of the uploaded bytes (processed_sha1).
    The annotations' album is skipped when the upload named an album
    explicitly. Errors are collected per sequence, never raised.
    """
    summary = ImmichStackSummary()
    for sequence in state_db.get_sequences(status="matched"):
        manifest = parse_manifest(json.loads(sequence["manifest_json"]))
        if manifest is None:
            continue
        album = None if album_override else manifest.annotations.album
        needs_stack = not sequence["immich_stack_id"]
        needs_album = bool(album) and sequence["immich_album"] != album

        members = state_db.get_stack_members(sequence["id"])
        merged = (
            state_db.get_photo_status(sequence["merged_inbox_path"])
            if sequence["merged_inbox_path"]
            else None
        )
        if any(m["status"] in IN_FLIGHT_STATUSES for m in members) or (
            merged and merged["status"] in IN_FLIGHT_STATUSES
        ):
            summary.waiting += 1
            continue
        # Failed or duplicate members have no asset of their own
        uploaded = [m for m in members if m["status"] == PhotoStatus.PROCESSED.value]
        uploaded.sort(key=lambda m: (not m["is_representative"], m["position"]))
        checksums = [m["processed_sha1"] for m in uploaded if m["processed_sha1"]]
        # The merged image is the cover; a stack made before it was uploaded
        # is made again with it first (Immich merges the two stacks)
        merged_sha1 = (
            merged["processed_sha1"]
            if merged and merged["status"] == PhotoStatus.PROCESSED.value
            else None
        )
        if merged_sha1:
            checksums.insert(0, merged_sha1)
            if sequence["immich_stack_primary"] != merged_sha1:
                needs_stack = True
                needs_album = bool(album)
        if not (needs_stack or needs_album):
            continue
        try:
            ids = uploader.asset_ids_by_checksum(api_key, checksums)
            if not checksums or any(c not in ids for c in checksums):
                summary.waiting += 1  # not on the server yet
                continue
            asset_ids = [ids[c] for c in checksums]
            if needs_stack and len(asset_ids) > 1:
                stack_id = uploader.create_stack(api_key, asset_ids)
                state_db.set_sequence_immich(
                    sequence["id"], stack_id=stack_id, primary_sha1=checksums[0]
                )
                summary.stacked += 1
            if needs_album and album:
                uploader.add_to_album(api_key, album, asset_ids)
                state_db.set_sequence_immich(sequence["id"], album=album)
                summary.albums += 1
        except Exception as e:
            summary.errors.append(f"{manifest.short_id}: {e}")
    return summary


@dataclass
class ExportSummary:
    exported: List[Path] = field(default_factory=list)
    skipped: int = 0
    missing: List[str] = field(default_factory=list)


def member_location(member: Dict, search_dirs: List[Path]) -> Optional[Path]:
    """Where a member's file lives now: its recorded location, else by name."""
    for candidate in (member["moved_to_path"], member["filepath"]):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    name = Path(member["filepath"]).name
    for directory in search_dirs:
        if (directory / name).is_file():
            return directory / name
    return None


def export_folder_name(manifest: Manifest) -> str:
    return f"{manifest.started_at:%Y-%m-%d_%H%M%S}_{manifest.kind}_{manifest.short_id}"


def export_stacks(
    state_db: ProcessingStateDB,
    out_dir: Path,
    search_dirs: List[Path],
    kind: Optional[str] = None,
    raw: Optional[bool] = None,
    force: bool = False,
) -> ExportSummary:
    """Copy each matched stack into its own folder, files named in frame order.

    raw=True keeps only RAW files, raw=False only JPEGs, None keeps both.
    Originals are never moved. A stack whose folder exists is skipped
    unless force is set.
    """
    summary = ExportSummary()
    for sequence in state_db.get_sequences(status="matched"):
        manifest = parse_manifest(json.loads(sequence["manifest_json"]))
        if manifest is None or (kind and manifest.kind != kind):
            continue
        dest = out_dir / export_folder_name(manifest)
        if dest.exists() and not force:
            summary.skipped += 1
            continue

        files = []
        for member in state_db.get_stack_members(manifest.id):
            suffix = Path(member["filepath"]).suffix.lower()
            if raw is True and suffix not in RAW_EXTENSIONS:
                continue
            if raw is False and suffix not in JPEG_SUFFIXES:
                continue
            source = member_location(member, search_dirs)
            if source is None:
                summary.missing.append(member["filepath"])
                continue
            files.append((source, f"{member['position']:03d}_{source.name}"))
        if not files:
            continue

        partial = dest.with_name(dest.name + ".partial")
        shutil.rmtree(partial, ignore_errors=True)
        partial.mkdir(parents=True)
        for source, name in files:
            shutil.copy2(source, partial / name)
        if dest.exists():
            shutil.rmtree(dest)
        partial.rename(dest)
        summary.exported.append(dest)
    return summary


MERGEABLE_KINDS = {"focus_stack"}


@dataclass
class MergedStack:
    sequence_id: str
    frames: int
    jpeg: Path
    tiff: Path
    depth: Path
    alignments: List[focus_merge.FrameAlignment]
    inbox_copy: Optional[Path] = None


@dataclass
class MergeSummary:
    merged: List[MergedStack] = field(default_factory=list)
    skipped: int = 0
    missing: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)


def merge_frames(
    state_db: ProcessingStateDB, sequence_id: str, search_dirs: List[Path]
) -> Tuple[List[Path], Optional[Path], List[str]]:
    """One file per exposure in frame order (RAW over JPEG), the
    representative's file, and the members no longer on disk."""
    members = state_db.get_stack_members(sequence_id)
    by_path = {m["filepath"]: m for m in members}
    frames: List[Path] = []
    representative: Optional[Path] = None
    missing: List[str] = []
    for group in group_pairs([m["filepath"] for m in members]):
        raws = [p for p in group if Path(p).suffix.lower() in RAW_EXTENSIONS]
        chosen = None
        for path in raws + [p for p in group if p not in raws]:
            chosen = member_location(by_path[path], search_dirs)
            if chosen is not None:
                break
        if chosen is None:
            missing.extend(group)
            continue
        frames.append(chosen)
        for path in group:
            if by_path[path]["is_representative"]:
                representative = member_location(by_path[path], search_dirs) or chosen
    return frames, representative, missing


def merged_name(manifest: Manifest) -> str:
    return f"{export_folder_name(manifest)}_merged"


def merged_user_comment(manifest: Manifest, frames: int) -> str:
    plan = manifest.plan_summary()
    return f"{manifest.kind} merged from {frames} frames" + (
        f", {plan}" if plan else ""
    )


def merged_keywords(manifest: Manifest) -> List[str]:
    kind = KIND_TAGS.get(manifest.kind, manifest.kind)
    return [f"{kind}_merged", f"stack_{manifest.short_id}"] + list(
        manifest.annotations.tags
    )


def tag_merged_image(
    exif: EXIFHandler, source: Path, target: Path, manifest: Manifest, frames: int
) -> None:
    """Metadata of the middle frame, plus the stack's keywords and annotations."""
    notes = manifest.annotations
    if not exif.copy_metadata_to_derived(
        source, target, merged_user_comment(manifest, frames)
    ):
        raise RuntimeError(f"could not copy metadata into {target.name}")
    if not exif.add_exif_metadata(
        target,
        rating=0,
        tags=merged_keywords(manifest),
        description=annotated_description("", notes),
        title=notes.title,
        lens=notes.lens,
    ):
        raise RuntimeError(f"could not write keywords into {target.name}")


def _inbox_destination(
    state_db: ProcessingStateDB, inbox_dir: Path, jpeg: Path
) -> Path:
    """A free inbox path, also unused as a photos key (an earlier merge's copy
    that already moved on keeps its record under that path)."""
    dest = unique_destination(inbox_dir, jpeg)
    if state_db.get_photo_status(str(dest)) is not None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = inbox_dir / f"{jpeg.stem}_{stamp}{jpeg.suffix}"
    return dest


def merge_stacks(
    state_db: ProcessingStateDB,
    out_dir: Path,
    search_dirs: List[Path],
    inbox_dir: Optional[Path] = None,
    sequence_id: Optional[str] = None,
    kind: str = "focus_stack",
    force: bool = False,
    exif: Optional[EXIFHandler] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> MergeSummary:
    """Merge each matched focus stack into one sharp image in out_dir.

    sequence_id matches the full id or its first 8 characters. A stack
    already merged (its JPEG still on disk) is skipped unless force is set.
    With inbox_dir, the merged JPEG is also copied there so the normal
    pipeline analyzes, tags and uploads it. Source frames are only read.
    """
    if kind not in MERGEABLE_KINDS:
        raise ValueError(f"only {', '.join(sorted(MERGEABLE_KINDS))} can be merged")
    say = progress or (lambda _: None)
    summary = MergeSummary()
    wanted = sequence_id.lower() if sequence_id else None
    for sequence in state_db.get_sequences(status="matched"):
        manifest = parse_manifest(json.loads(sequence["manifest_json"]))
        if manifest is None or manifest.kind != kind:
            continue
        if wanted and wanted not in (manifest.id.lower(), manifest.short_id):
            continue
        merged_path = sequence["merged_path"]
        if merged_path and Path(merged_path).is_file() and not force:
            summary.skipped += 1
            continue

        frames, representative, missing = merge_frames(
            state_db, manifest.id, search_dirs
        )
        summary.missing.extend(missing)
        if len(frames) < 2 or representative is None:
            summary.failed.append(f"{manifest.short_id}: fewer than two frames")
            continue
        middle = next(
            (i for i, f in enumerate(frames) if f.stem == representative.stem),
            (len(frames) - 1) // 2,
        )
        try:
            say(f"{manifest.short_id}: merging {len(frames)} frames")
            result = focus_merge.merge(
                frames, middle, lambda step: say(f"{manifest.short_id}: {step}")
            )
            tiff, jpeg, depth = focus_merge.write_outputs(
                result, out_dir / merged_name(manifest), len(frames)
            )
            alignments = result.alignments
            del result
            for target in (tiff, jpeg):
                tag_merged_image(
                    exif or EXIFHandler(), representative, target, manifest, len(frames)
                )
        except Exception as e:
            summary.failed.append(f"{manifest.short_id}: {e}")
            continue

        inbox_copy = None
        if inbox_dir is not None:
            inbox_dir.mkdir(parents=True, exist_ok=True)
            inbox_copy = _inbox_destination(state_db, inbox_dir, jpeg)
            shutil.copy2(jpeg, inbox_copy)
        state_db.set_sequence_merged(
            manifest.id, str(jpeg), str(inbox_copy) if inbox_copy else None
        )
        summary.merged.append(
            MergedStack(
                manifest.id,
                len(frames),
                jpeg,
                tiff,
                depth,
                alignments,
                inbox_copy=inbox_copy,
            )
        )
    return summary
