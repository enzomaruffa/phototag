"""Abstract base class for AI services."""

from abc import ABC, abstractmethod
from typing import List, Optional
from pathlib import Path

from ..models.ai import AIAnalysisResponse


class AIService(ABC):
    """Abstract base class for AI photo analysis services."""

    @abstractmethod
    async def analyze_photo(
        self, image_path: Path, existing_tags: Optional[List[str]] = None
    ) -> AIAnalysisResponse:
        """Analyze a photo and return structured response."""
        pass

    @abstractmethod
    def health_check(self) -> bool:
        """Check if the AI service is available."""
        pass
