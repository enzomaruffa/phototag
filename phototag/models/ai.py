"""Pydantic models for AI analysis responses."""

from typing import List, Optional
from pydantic import BaseModel, Field


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


class ProcessedPhoto(BaseModel):
    """Processed photo with metadata."""

    file_path: str
    rating: int
    tags: List[str]
    description: str
    notes: Optional[str] = None
    needs_review: bool = False
    new_tags: List[str] = Field(default_factory=list)
