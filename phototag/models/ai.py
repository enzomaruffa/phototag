"""Pydantic models for AI analysis responses."""

import re
import unicodedata
from typing import Any, List, Optional

from pydantic import BaseModel, Field, field_validator

SUBJECT_KINDS = (
    "artwork",
    "landmark",
    "building",
    "place",
    "species",
    "product",
    "vehicle",
    "event",
    "public_figure",
    "other",
)

# Below this the prompt tells the model to leave a subject out; anything that
# slips through anyway is not written as a keyword.
MIN_SUBJECT_CONFIDENCE = 0.7


def subject_tag(name: str) -> str:
    """Proper name -> snake_case keyword ('Porsche 911 (992)' -> 'porsche_911_992')."""
    ascii_name = (
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    )
    return re.sub(r"[^a-z0-9]+", "_", ascii_name.lower()).strip("_")


class NamedSubject(BaseModel):
    """A specific, recognizable thing in the photo, identified by proper name."""

    name: str
    kind: str = "other"
    confidence: float = 0.0

    @field_validator("kind", mode="before")
    @classmethod
    def _known_kind(cls, value: Any) -> str:
        kind = str(value or "").strip().lower().replace(" ", "_")
        return kind if kind in SUBJECT_KINDS else "other"

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, value: Any) -> float:
        # A validation error here would fail the response and trigger a
        # retry, which bills the photo a second time.
        try:
            return min(max(float(value), 0.0), 1.0)
        except (TypeError, ValueError):
            return 0.0


class AIAnalysisResponse(BaseModel):
    """Response from AI photo analysis."""

    rating: int = Field(..., ge=1, le=5, description="Photo rating 1-5 stars")
    existing_tags_used: List[str] = Field(
        default_factory=list, description="Existing tags that apply"
    )
    new_tags_needed: List[str] = Field(
        default_factory=list, description="New tags to create"
    )
    description: str = Field(
        ..., description="Detailed description of the image content"
    )
    notes: Optional[str] = Field(None, description="Analysis notes or description")
    confidence: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="AI confidence score"
    )
    visible_date: Optional[str] = Field(
        None,
        description="Date stamp printed/burned into the image (YYYY-MM-DD), if clearly legible",
    )
    named_subjects: List[NamedSubject] = Field(
        default_factory=list,
        description="Recognizable subjects identified by proper name",
    )

    def named_subject_tags(self) -> List[str]:
        """Keywords for the confidently named subjects, deduplicated, in order."""
        tags: List[str] = []
        for subject in self.named_subjects:
            if subject.confidence < MIN_SUBJECT_CONFIDENCE:
                continue
            tag = subject_tag(subject.name)
            if tag and tag not in tags:
                tags.append(tag)
        return tags


def keywords_to_write(
    analysis: AIAnalysisResponse,
    approved_tags: List[str],
    extra_tags: Optional[List[str]] = None,
) -> List[str]:
    """Every EXIF keyword for a photo.

    Vocabulary tags only count once approved. Named subjects and extra tags
    (stack membership) are proper nouns or system tags, so they skip review.
    """
    approved = set(approved_tags)
    keywords: List[str] = []
    for tag in (
        [
            t
            for t in analysis.existing_tags_used + analysis.new_tags_needed
            if t in approved
        ]
        + analysis.named_subject_tags()
        + (extra_tags or [])
    ):
        if tag not in keywords:
            keywords.append(tag)
    return keywords


class ProcessedPhoto(BaseModel):
    """Processed photo with metadata."""

    file_path: str
    rating: int
    tags: List[str]
    description: str
    notes: Optional[str] = None
    needs_review: bool = False
    new_tags: List[str] = Field(default_factory=list)
