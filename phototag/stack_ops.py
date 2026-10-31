"""Stack workflows over the state DB: import manifests and match inbox photos.

The matching rules live in phototag.stacks; this module feeds them EXIF
data and persists the results.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .media import find_media_files
from .stacks import (
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
from .storage.state_db import ProcessingStateDB

logger = logging.getLogger(__name__)

DEFAULT_MANIFEST_DIR = "~/Library/Mobile Documents/iCloud~dev~enzomaruffa~openalphalink/Documents/Sequences"
DEFAULT_MAX_SKEW_S = 120.0


def manifest_dirs() -> List[Path]:
    """SEQUENCE_MANIFEST_DIR as paths (comma list allowed)."""
    raw = os.getenv("SEQUENCE_MANIFEST_DIR") or DEFAULT_MANIFEST_DIR
    return [Path(d.strip()).expanduser() for d in raw.split(",") if d.strip()]


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
    matched: List[str] = field(default_factory=list)
    unmatched: List[str] = field(default_factory=list)


def import_manifests(
    state_db: ProcessingStateDB, directories: List[Path]
) -> ImportSummary:
    """Store every manifest found; re-importing a known id is a no-op."""
    summary = ImportSummary()
    for path in find_manifest_files(directories):
        manifest = load_manifest(path)
        if manifest is None:
            continue  # some other JSON file
        if state_db.import_sequence(
            manifest.id,
            manifest.kind,
            path.read_text(),
            str(path),
            manifest.started_at.isoformat(),
            len(manifest.frames),
        ):
            summary.imported += 1
        else:
            summary.already_known += 1
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

    taken = state_db.get_stack_member_paths()
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
    is_representative: bool
    representative_analysis: Optional[str]  # saved ai_response_json


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
        is_representative=bool(row["is_representative"]),
        representative_analysis=row["representative_ai_response_json"],
    )
