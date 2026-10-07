"""Memory-card discovery and import into the inbox (macOS)."""

import os
import plistlib
import shutil
import subprocess
from pathlib import Path
from typing import List, NamedTuple, Optional

from phototag.media import MEDIA_EXTENSIONS, file_hashes, pixel_hash
from phototag.storage.exif import EXIFHandler
from phototag.storage.state_db import ProcessingStateDB

# Camera-generated previews that sit next to the real clips (Sony M4ROOT/THMBNL).
SKIPPED_DIRS = {"THMBNL"}


class CardVolume(NamedTuple):
    path: Path
    name: str
    size_bytes: int


def _diskutil_info(path: Path) -> Optional[dict]:
    try:
        out = subprocess.run(
            ["diskutil", "info", "-plist", str(path)],
            capture_output=True,
            check=True,
        ).stdout
        return plistlib.loads(out)
    except (OSError, subprocess.CalledProcessError, plistlib.InvalidFileException):
        return None


def find_removable_volumes() -> List[CardVolume]:
    """Mounted removable/ejectable volumes: SD cards, card readers, USB drives."""
    volumes = []
    for path in sorted(Path("/Volumes").iterdir()):
        if not path.is_dir() or path.is_symlink():
            continue
        info = _diskutil_info(path)
        if not info or not (info.get("RemovableMedia") or info.get("Ejectable")):
            continue
        volumes.append(
            CardVolume(
                path=path,
                name=info.get("VolumeName") or path.name,
                size_bytes=int(info.get("TotalSize") or 0),
            )
        )
    return volumes


def find_card_media(root: Path) -> List[Path]:
    """Photos and videos on a card, skipping hidden files and camera thumbnails."""
    media = []
    for f in root.rglob("*"):
        rel_parts = f.relative_to(root).parts
        if any(p.startswith(".") or p in SKIPPED_DIRS for p in rel_parts):
            continue
        if f.suffix.lower() in MEDIA_EXTENSIONS and f.is_file():
            media.append(f)
    return sorted(media)


def is_known(path: Path, state_db: ProcessingStateDB, exif: EXIFHandler) -> bool:
    """Same dedup check as intake: already in the pipeline, processed, or in Immich."""
    hashes = file_hashes(path)
    if hashes is None:
        return False
    if state_db.find_duplicate(hashes.sha256, pixel_hash=pixel_hash(path)):
        return True
    if state_db.has_immich_checksum(hashes.sha1):
        return True
    return _matches_unhashed_copy(path, state_db, exif)


def _matches_unhashed_copy(
    path: Path, state_db: ProcessingStateDB, exif: EXIFHandler
) -> bool:
    """Same filename and capture time as an unhashed processed photo still on disk.

    The capture time guards against camera filename counters wrapping around.
    """
    if not state_db.has_unhashed_processed(path.name):
        return False
    captured = exif.get_capture_date(path)
    if captured is None:
        return False
    for folder in (
        os.getenv("OUTBOX_DIR", "./outbox"),
        os.getenv("PROCESSED_DIR", "./processed"),
    ):
        copy = Path(folder) / path.name
        if copy.exists() and exif.get_capture_date(copy) == captured:
            return True
    return False


def copy_to_inbox(src: Path, dest: Path) -> None:
    """Copy keeping the card's mtime, renaming into place only once complete.

    The '.partial' name has no media extension, so an interrupted copy is never
    picked up by 'process'.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".partial")
    shutil.copy2(src, tmp)
    tmp.rename(dest)


def eject(volume: Path) -> bool:
    result = subprocess.run(["diskutil", "eject", str(volume)], capture_output=True)
    return result.returncode == 0
