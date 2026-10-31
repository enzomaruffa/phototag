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
from .storage.immich import ImmichUploader
from .storage.state_db import PhotoStatus, ProcessingStateDB

logger = logging.getLogger(__name__)

DEFAULT_MANIFEST_DIR = "~/Library/Mobile Documents/iCloud~dev~enzomaruffa~openalphalink/Documents/Sequences"
DEFAULT_MAX_SKEW_S = 120.0

# Member statuses whose files already carry their EXIF; annotations edited
# later are not rewritten into them
WRITTEN_STATUSES = {
    PhotoStatus.EXIF_WRITTEN.value,
    PhotoStatus.MOVING.value,
    PhotoStatus.PROCESSED.value,
}


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


def member_keywords(stack: Optional[MemberContext]) -> List[str]:
    """Keywords a stack member gets on top of its own analysis."""
    if not stack:
        return []
    return stack.manifest.tags + list(stack.manifest.annotations.tags)


def member_description(description: str, stack: Optional[MemberContext]) -> str:
    """AI description with the user's subject kept and their notes appended."""
    if not stack:
        return description
    notes = stack.manifest.annotations
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
        if not (needs_stack or needs_album):
            continue

        members = state_db.get_stack_members(sequence["id"])
        if any(m["status"] in IN_FLIGHT_STATUSES for m in members):
            summary.waiting += 1
            continue
        # Failed or duplicate members have no asset of their own
        uploaded = [m for m in members if m["status"] == PhotoStatus.PROCESSED.value]
        uploaded.sort(key=lambda m: (not m["is_representative"], m["position"]))
        checksums = [m["processed_sha1"] for m in uploaded if m["processed_sha1"]]
        try:
            ids = uploader.asset_ids_by_checksum(api_key, checksums)
            if not checksums or any(c not in ids for c in checksums):
                summary.waiting += 1  # not on the server yet
                continue
            asset_ids = [ids[c] for c in checksums]
            if needs_stack and len(asset_ids) > 1:
                stack_id = uploader.create_stack(api_key, asset_ids)
                state_db.set_sequence_immich(sequence["id"], stack_id=stack_id)
                summary.stacked += 1
            if needs_album and album:
                uploader.add_to_album(api_key, album, asset_ids)
                state_db.set_sequence_immich(sequence["id"], album=album)
                summary.albums += 1
        except Exception as e:
            summary.errors.append(f"{manifest.short_id}: {e}")
    return summary
