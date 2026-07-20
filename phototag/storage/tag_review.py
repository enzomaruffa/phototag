"""Tag review system with persistent storage."""

import json
import threading
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime
from contextlib import contextmanager

from ..models.tags import TagReviewSession, PendingTag


class TagReviewStorage:
    """Manages storage and retrieval of pending tags for review."""

    def __init__(self, storage_dir: Path = Path.cwd() / ".phototag"):
        self.storage_dir = storage_dir
        self.storage_dir.mkdir(exist_ok=True)
        self.review_file = self.storage_dir / "pending_tags.json"
        self.approved_tags_file = self.storage_dir / "approved_tags.json"
        self._lock = threading.RLock()  # Reentrant lock for thread safety

    @contextmanager
    def _file_lock(self):
        """Context manager for thread-safe file operations."""
        with self._lock:
            yield

    def load_pending_session(self) -> TagReviewSession:
        """Load current pending tag session."""
        with self._file_lock():
            if not self.review_file.exists():
                return TagReviewSession()

            try:
                with open(self.review_file, "r") as f:
                    data = json.load(f)
                return TagReviewSession(**data)
            except Exception:
                # If corrupted, start fresh
                return TagReviewSession()

    def save_pending_session(self, session: TagReviewSession):
        """Save current pending tag session."""
        with self._file_lock():
            with open(self.review_file, "w") as f:
                json.dump(session.model_dump(), f, indent=2, default=str)

    def add_pending_tag(
        self, tag_name: str, photo_path: str, confidence: Optional[float] = None
    ):
        """Add a new pending tag."""
        with self._file_lock():
            session = self.load_pending_session()

            # Check if tag already exists
            existing = next(
                (t for t in session.pending_tags if t.name == tag_name), None
            )
            if not existing:
                pending_tag = PendingTag(
                    name=tag_name, suggested_by_photo=photo_path, confidence=confidence
                )
                session.add_pending_tag(pending_tag, photo_path)
                self.save_pending_session(session)

    def has_pending_tags(self) -> bool:
        """Check if there are tags pending review."""
        session = self.load_pending_session()
        return len(session.pending_tags) > 0

    def get_pending_tags(self) -> List[PendingTag]:
        """Get all pending tags."""
        session = self.load_pending_session()
        return session.pending_tags

    def approve_tags(self, approved_tag_names: List[str]) -> List[str]:
        """Approve tags and return list of photos that need EXIF updates."""
        with self._file_lock():
            session = self.load_pending_session()
            photos_to_update = []

            # Track approved tags
            approved_tags = self.load_approved_tags()

            for tag_name in approved_tag_names:
                # Find the pending tag
                pending_tag = next(
                    (t for t in session.pending_tags if t.name == tag_name), None
                )
                if pending_tag:
                    # Add to approved tags
                    approved_tags.append(
                        {
                            "name": tag_name,
                            "approved_at": datetime.now().isoformat(),
                            "suggested_by": pending_tag.suggested_by_photo,
                        }
                    )

                    # Track which photos need updates
                    if pending_tag.suggested_by_photo not in photos_to_update:
                        photos_to_update.append(pending_tag.suggested_by_photo)

                    # Remove from pending
                    session.pending_tags = [
                        t for t in session.pending_tags if t.name != tag_name
                    ]

            # Save updates
            self.save_approved_tags(approved_tags)
            self.save_pending_session(session)

            return photos_to_update

    def reject_tags(self, rejected_tag_names: List[str]):
        """Reject tags and remove from pending."""
        with self._file_lock():
            session = self.load_pending_session()
            session.pending_tags = [
                t for t in session.pending_tags if t.name not in rejected_tag_names
            ]
            self.save_pending_session(session)

    def load_approved_tags(self) -> List[Dict]:
        """Load list of approved tags."""
        with self._file_lock():
            if not self.approved_tags_file.exists():
                return []

            try:
                with open(self.approved_tags_file, "r") as f:
                    return json.load(f)
            except Exception:
                return []

    def save_approved_tags(self, approved_tags: List[Dict]):
        """Save approved tags list."""
        with self._file_lock():
            with open(self.approved_tags_file, "w") as f:
                json.dump(approved_tags, f, indent=2)

    def get_approved_tag_names(self) -> List[str]:
        """Get list of approved tag names."""
        approved = self.load_approved_tags()
        return [tag["name"] for tag in approved]

    def clear_pending(self):
        """Clear all pending tags (for testing/reset)."""
        if self.review_file.exists():
            self.review_file.unlink()

    def get_photos_for_tag(self, tag_name: str) -> List[str]:
        """Get list of photos that would be affected by approving this tag."""
        session = self.load_pending_session()
        return session.photos_affected

    def get_pending_tag_names(self) -> List[str]:
        """Get list of pending tag names."""
        pending = self.get_pending_tags()
        return [tag.name for tag in pending]

    def get_all_available_tags(self) -> List[str]:
        """Get combined list of approved and pending tag names for AI context."""
        approved = self.get_approved_tag_names()
        pending = self.get_pending_tag_names()
        return list(set(approved + pending))  # Remove duplicates
