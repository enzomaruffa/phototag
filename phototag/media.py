"""Shared media-type helpers used by the CLI and the processor."""

import hashlib
import time
from datetime import datetime
from pathlib import Path
from typing import List, NamedTuple, Optional

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tiff", ".tif"}
RAW_EXTENSIONS = {".arw", ".cr2", ".nef", ".dng", ".raw"}
VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".avi",
    ".m4v",
    ".mpg",
    ".mpeg",
    ".mts",
    ".m2ts",
    ".3gp",
    ".wmv",
    ".webm",
    ".mkv",
}

IMAGE_EXTENSIONS = PHOTO_EXTENSIONS | RAW_EXTENSIONS
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


def is_video(path: Path) -> bool:
    """True if the file is a video (passed through the pipeline without AI tagging)."""
    return path.suffix.lower() in VIDEO_EXTENSIONS


def find_media_files(directory: Path, include_videos: bool = True) -> List[Path]:
    """Find supported media files under a directory."""
    extensions = MEDIA_EXTENSIONS if include_videos else IMAGE_EXTENSIONS
    return sorted(f for f in directory.rglob("*") if f.suffix.lower() in extensions)


def is_stable(path: Path, min_age_seconds: float = 30.0) -> bool:
    """True if the file hasn't been modified recently.

    Files delivered by sync tools (Syncthing, Dropbox, ...) can exist on disk
    before their content is complete; processing them mid-sync fails with
    unreadable-image errors. Waiting until mtime is at least this old avoids that.
    """
    try:
        return (time.time() - path.stat().st_mtime) >= min_age_seconds
    except OSError:
        return False


class FileHashes(NamedTuple):
    sha256: str  # local identity across re-syncs
    sha1: str  # comparable to Immich's asset checksums


def file_hashes(path: Path) -> Optional[FileHashes]:
    """Content hashes of the file, streamed in one pass; None if unreadable.

    Hashed at intake, BEFORE the EXIF write mutates the bytes - the original
    content is the only stable identity a photo has across re-syncs. (This is
    also why Immich's server-side dedup can't catch re-synced originals of
    photos WE uploaded: the uploaded copy has different bytes after the EXIF
    write.) The SHA-1 exists solely because Immich checksums assets with
    SHA-1, letting us recognize files that reached the server via other
    clients (phone app, web) - see 'phototag immich-sync'.
    """
    try:
        sha256 = hashlib.sha256()
        sha1 = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                sha256.update(chunk)
                sha1.update(chunk)
        return FileHashes(sha256.hexdigest(), sha1.hexdigest())
    except OSError:
        return None


def unique_destination(directory: Path, source: Path) -> Path:
    """Destination path inside directory, timestamped on name conflict."""
    dest = directory / source.name
    if dest.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = directory / f"{source.stem}_{timestamp}{source.suffix}"
    return dest
