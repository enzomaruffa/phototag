"""Capture-date inference for cameras that write no EXIF (toy cams, film scans).

Some sources (Kodak Charmera and other keychain cams, lab film scans) produce
JPEGs with no DateTimeOriginal at all. Without a date, Immich sorts them by
upload time, which scatters a batch across the timeline. This module infers a
usable capture date and preserves shooting order:

resolution chain (first hit wins):
  1. "exif"      - the file already has a capture date; never overwritten
  2. "stamp"     - a date printed/burned into the image, read by the AI pass
  3. "neighbour" - the nearest same-folder photo (by filename sequence) that
                   already resolved a date anchors this one
  4. "mtime"     - file modification time, as a weak last resort

Shooting order: the trailing number in the filename (PICT0043 -> 43,
"roll - 12.jpg" -> 12) offsets the resolved date by seconds, so _2 always
sorts after _1 even when both inherit the same anchor date.
"""

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

MIN_PLAUSIBLE_YEAR = 1970


def filename_sequence(path: Path) -> Optional[int]:
    """Trailing number in the filename stem (PICT0043 -> 43); None if absent."""
    numbers = re.findall(r"\d+", path.stem)
    return int(numbers[-1]) if numbers else None


def parse_visible_date(value: Optional[str]) -> Optional[datetime]:
    """AI-reported date stamp -> datetime at noon, if plausible.

    Noon (not midnight) so timezone display shifts in Immich can't move the
    photo to the previous/next day.
    """
    if not value:
        return None
    match = re.search(r"(\d{4})\D(\d{1,2})\D(\d{1,2})", value.strip())
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    try:
        parsed = datetime(year, month, day, 12, 0, 0)
    except ValueError:
        return None
    if not (MIN_PLAUSIBLE_YEAR <= year <= datetime.now().year + 1):
        return None
    return parsed


def exiftool_to_iso(value: str) -> Optional[str]:
    """exiftool date string ('2026:07:19 22:14:46[+TZ]') -> ISO, or None."""
    match = re.match(r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}:\d{2}:\d{2})", value.strip())
    if not match:
        return None
    year, month, day, time_part = match.groups()
    try:
        return datetime.fromisoformat(f"{year}-{month}-{day}T{time_part}").isoformat()
    except ValueError:
        return None


def source_context(width: int, height: int, file_size_bytes: int) -> str:
    """Prompt clause describing the capture source, inferred from measurables.

    Low-res files come from toy cameras, old digicams, or heavy compression -
    the AI should judge those relative to their device class instead of
    penalizing inherent softness, and should look for printed date stamps
    (which those same devices tend to burn into the frame).
    """
    megapixels = (width * height) / 1_000_000
    size_kb = file_size_bytes // 1024
    context = (
        f"SOURCE CONTEXT: the original file is {width}x{height} "
        f"({megapixels:.1f} MP, {size_kb} KB).\n"
    )
    if megapixels < 2.5:
        context += (
            "This resolution indicates a toy/keychain camera, very old digicam, or "
            "low-quality scan. Softness, noise, muted color, and compression "
            "artifacts are inherent to this device class - do NOT reduce the rating "
            "for them. Rate composition, moment, and subject interest relative to "
            "what such a device can capture, and prefer quality tags (e.g. 'lofi', "
            "'blur') over low ratings for device-inherent flaws.\n"
        )
    elif megapixels < 8:
        context += (
            "This resolution suggests an older digital camera or small sensor; "
            "judge technical quality relative to that class of device.\n"
        )
    return context


def resolve_capture_date(
    photo_path: Path,
    has_exif_date: bool,
    visible_date: Optional[str],
    neighbours: List[Tuple[str, Optional[str], str]],
) -> Optional[Tuple[datetime, str]]:
    """(capture datetime, source) for a photo, or None to leave it untouched.

    neighbours: (filepath, capture_date ISO string, capture_date_source) rows
    for already-resolved photos in the same original folder.
    """
    if has_exif_date:
        return None  # the camera knew best; never overwrite

    sequence = filename_sequence(photo_path)

    stamped = parse_visible_date(visible_date)
    if stamped is not None:
        # Same-day photos share the stamp date; the sequence spreads them out
        return stamped + timedelta(seconds=sequence or 0), "stamp"

    anchor = _nearest_anchor(photo_path, sequence, neighbours)
    if anchor is not None:
        anchor_date, anchor_seq = anchor
        delta = (sequence - anchor_seq) if (sequence is not None) else 1
        return anchor_date + timedelta(seconds=delta), "neighbour"

    try:
        mtime = datetime.fromtimestamp(photo_path.stat().st_mtime)
    except OSError:
        return None
    if mtime.year < MIN_PLAUSIBLE_YEAR:
        return None
    return mtime, "mtime"


def _nearest_anchor(
    photo_path: Path,
    sequence: Optional[int],
    neighbours: List[Tuple[str, Optional[str], str]],
) -> Optional[Tuple[datetime, int]]:
    """Nearest same-folder photo (by sequence distance) usable as a date anchor.

    Photos that themselves inherited via 'neighbour' or 'mtime' are not
    anchors - chaining guesses onto guesses compounds error.
    """
    candidates = []
    for filepath, capture_date, source in neighbours:
        if source not in ("exif", "stamp") or not capture_date:
            continue
        other = Path(filepath)
        if other == photo_path:
            continue
        other_seq = filename_sequence(other)
        if other_seq is None:
            continue
        try:
            other_date = datetime.fromisoformat(capture_date)
        except ValueError:
            continue
        distance = abs((sequence or 0) - other_seq)
        # Prefer the nearest; tie-break toward the earlier neighbour
        candidates.append((distance, other_seq, other_date))

    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))
    _, anchor_seq, anchor_date = candidates[0]
    return anchor_date, anchor_seq
