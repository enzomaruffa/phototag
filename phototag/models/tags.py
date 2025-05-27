"""Pydantic models for tag review system."""

from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel, Field


class PendingTag(BaseModel):
    """A tag pending review."""

    name: str
    category: Optional[str] = None
    suggested_by_photo: str
    created_at: datetime = Field(default_factory=datetime.now)
    confidence: Optional[float] = None


class TagReview(BaseModel):
    """Review decision for pending tags."""

    tag_name: str
    approved: bool
    reason: Optional[str] = None
    reviewed_at: datetime = Field(default_factory=datetime.now)


class TagReviewSession(BaseModel):
    """Collection of pending tags for review."""

    pending_tags: List[PendingTag] = Field(default_factory=list)
    photos_affected: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)

    def add_pending_tag(self, tag: PendingTag, photo_path: str):
        """Add a pending tag and track which photo it affects."""
        if tag.name not in [t.name for t in self.pending_tags]:
            self.pending_tags.append(tag)
        if photo_path not in self.photos_affected:
            self.photos_affected.append(photo_path)
