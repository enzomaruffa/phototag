"""EXIF metadata handling for photos using exiftool."""

from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any
import logging
import subprocess
import traceback
import json


class EXIFHandler:
    """Handles EXIF metadata writing and reading using exiftool."""

    def __init__(self):
        """Initialize EXIF handler."""
        self.exiftool_path = "exiftool"  # Assumes exiftool is in PATH
        # Verify exiftool is available
        try:
            subprocess.run(
                [self.exiftool_path, "-ver"], capture_output=True, check=True
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            raise RuntimeError(
                "exiftool not found. Please install exiftool and ensure it's in your PATH"
            )

    def add_exif_metadata(
        self,
        photo_path: Path,
        rating: int,
        tags: List[str],
        description: str,
        notes: Optional[str] = None,
    ) -> bool:
        """Add metadata to photo's EXIF data without overwriting existing data."""
        try:
            logging.info(f"Adding EXIF metadata to: {photo_path}")

            # Build command
            cmd = [self.exiftool_path, "-overwrite_original"]

            # Read existing metadata first to check what's already there
            existing = self._read_metadata_raw(photo_path)

            # Rating (1-5 stars) - only add if not already present
            if rating and 1 <= rating <= 5:
                if not existing.get("Rating") or existing.get("Rating") == "0":
                    cmd.extend([f"-Rating={rating}"])
                    logging.info(f"Adding rating: {rating}")

            # Description - only add if not already present
            if description:
                if not existing.get("ImageDescription", "").strip():
                    # Write to multiple fields for better compatibility
                    cmd.extend([f"-ImageDescription={description}"])
                    cmd.extend([f"-Description={description}"])
                    # Also write to IPTC Caption for Lightroom
                    cmd.extend([f"-IPTC:Caption-Abstract={description}"])
                    logging.info(f"Adding description: {description}")

            # Keywords/Tags - merge with existing
            if tags:
                existing_keywords = existing.get("Keywords", [])
                if isinstance(existing_keywords, str):
                    existing_keywords = [existing_keywords]

                # Convert to lowercase for comparison
                existing_lower = [k.lower() for k in existing_keywords]

                # Add new unique tags
                for tag in tags:
                    tag = tag.strip()
                    if tag and tag.lower() not in existing_lower:
                        cmd.extend([f"-Keywords+={tag}"])
                        existing_lower.append(tag.lower())

                logging.info(f"Adding keywords: {tags}")

            # Notes - only add if not already present
            if notes:
                if not existing.get("UserComment", "").strip():
                    cmd.extend([f"-UserComment={notes}"])
                    logging.info("Adding notes")

            # Only execute if we have something to add
            if len(cmd) > 3:  # More than just exiftool -overwrite_original
                cmd.append(str(photo_path))

                # Execute command
                result = subprocess.run(cmd, capture_output=True, text=True)

                if result.returncode == 0:
                    logging.info(f"Successfully added EXIF metadata to: {photo_path}")
                    return True
                else:
                    logging.error(f"exiftool error: {result.stderr}")
                    return False
            else:
                logging.info("No new metadata to add")
                return True

        except Exception as e:
            logging.error(f"Failed to add EXIF metadata to {photo_path}: {e}")
            logging.error(f"Full traceback: {traceback.format_exc()}")
            return False

    def update_exif_tags(self, photo_path: Path, new_tags: List[str]) -> bool:
        """Add new tags to existing EXIF without overwriting."""
        try:
            logging.info(f"Updating tags for: {photo_path}")

            # Read existing metadata
            existing = self._read_metadata_raw(photo_path)
            existing_keywords = existing.get("Keywords", [])
            if isinstance(existing_keywords, str):
                existing_keywords = [existing_keywords]

            # Find unique new tags
            existing_lower = [k.lower() for k in existing_keywords]
            unique_new_tags = []

            cmd = [self.exiftool_path, "-overwrite_original"]

            for tag in new_tags:
                tag = tag.strip()
                if tag and tag.lower() not in existing_lower:
                    cmd.extend([f"-Keywords+={tag}"])
                    unique_new_tags.append(tag)
                    existing_lower.append(tag.lower())

            if not unique_new_tags:
                logging.info("No new unique tags to add")
                return True

            # Execute command
            cmd.append(str(photo_path))
            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode == 0:
                logging.info(f"Updated EXIF with {len(unique_new_tags)} new tags")
                return True
            else:
                logging.error(f"exiftool error: {result.stderr}")
                return False

        except Exception as e:
            logging.error(f"Failed to update EXIF tags for {photo_path}: {e}")
            return False

    def read_exif_metadata(self, photo_path: Path) -> Optional[Dict[str, Any]]:
        """Read metadata from photo's EXIF data."""
        try:
            raw_metadata = self._read_metadata_raw(photo_path)

            # Convert to our standard format
            metadata = {
                "description": raw_metadata.get("ImageDescription")
                or raw_metadata.get("Description"),
                "rating": raw_metadata.get("Rating"),
                "tags": [],
                "notes": raw_metadata.get("UserComment"),
            }

            # Handle keywords/tags
            keywords = raw_metadata.get("Keywords", [])
            if isinstance(keywords, str):
                metadata["tags"] = [keywords]
            elif isinstance(keywords, list):
                metadata["tags"] = keywords

            # Convert rating to int if present
            if metadata["rating"] is not None:
                try:
                    metadata["rating"] = int(metadata["rating"])
                except (ValueError, TypeError):
                    metadata["rating"] = None

            return metadata

        except Exception as e:
            logging.error(f"Failed to read EXIF from {photo_path}: {e}")
            return None

    def _read_metadata_raw(self, photo_path: Path) -> Dict[str, Any]:
        """Read raw metadata using exiftool -j."""
        try:
            cmd = [self.exiftool_path, "-j", str(photo_path)]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)

            # Parse JSON output
            metadata_list = json.loads(result.stdout)
            if metadata_list and len(metadata_list) > 0:
                return metadata_list[0]
            else:
                return {}

        except subprocess.CalledProcessError as e:
            logging.error(f"exiftool error reading {photo_path}: {e.stderr}")
            return {}
        except json.JSONDecodeError as e:
            logging.error(f"Failed to parse exiftool JSON output: {e}")
            return {}

    def get_capture_date(self, photo_path: Path) -> Optional[str]:
        """The file's existing capture date (raw exiftool string), if any."""
        existing = self._read_metadata_raw(photo_path)
        for key in ("DateTimeOriginal", "CreateDate", "DateCreated"):
            value = existing.get(key)
            if value and str(value).strip() and not str(value).startswith("0000"):
                return str(value)
        return None

    def set_capture_date(self, photo_path: Path, capture_date: datetime) -> bool:
        """Write DateTimeOriginal/CreateDate for files that have none.

        Used for dateless sources (toy cameras, film scans) after the capture
        date has been inferred - see phototag.dating. Callers check
        get_capture_date() first; this never merges, it just writes.
        """
        try:
            stamp = capture_date.strftime("%Y:%m:%d %H:%M:%S")
            cmd = [
                self.exiftool_path,
                "-overwrite_original",
                f"-DateTimeOriginal={stamp}",
                f"-CreateDate={stamp}",
                str(photo_path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                logging.info(f"Set capture date {stamp} on {photo_path.name}")
                return True
            logging.error(f"exiftool error setting capture date: {result.stderr}")
            return False
        except Exception as e:
            logging.error(f"Failed to set capture date on {photo_path}: {e}")
            return False

    def remove_zero_rating(self, photo_path: Path) -> bool:
        """Remove rating if it's set to 0."""
        try:
            existing = self._read_metadata_raw(photo_path)

            if existing.get("Rating") == 0 or existing.get("Rating") == "0":
                cmd = [
                    self.exiftool_path,
                    "-overwrite_original",
                    "-Rating=",
                    str(photo_path),
                ]
                result = subprocess.run(cmd, capture_output=True, text=True)

                if result.returncode == 0:
                    logging.info(f"Removed zero rating from: {photo_path}")
                    return True
                else:
                    logging.error(f"Failed to remove zero rating: {result.stderr}")
                    return False

            return True

        except Exception as e:
            logging.error(f"Failed to remove zero rating from {photo_path}: {e}")
            return False

    def copy_metadata(self, source_path: Path, dest_path: Path) -> bool:
        """Copy all metadata from source to destination."""
        try:
            cmd = [
                self.exiftool_path,
                "-overwrite_original",
                "-TagsFromFile",
                str(source_path),
                "-all:all",
                str(dest_path),
            ]

            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode == 0:
                logging.info(f"Copied metadata from {source_path} to {dest_path}")
                return True
            else:
                logging.error(f"Failed to copy metadata: {result.stderr}")
                return False

        except Exception as e:
            logging.error(f"Failed to copy metadata: {e}")
            return False
