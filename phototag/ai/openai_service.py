"""OpenAI implementation of AI service."""

import asyncio
import base64
import io
import json
from pathlib import Path
from typing import List, Optional

from openai import AsyncOpenAI
from PIL import Image
import rawpy

from .base import AIService
from ..models.ai import AIAnalysisResponse


class OpenAIService(AIService):
    """OpenAI implementation for photo analysis."""

    def __init__(self, api_key: str, model: str = "gpt-4o"):
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model

    def _encode_image(self, image_path: Path) -> str:
        """Encode image to base64 for OpenAI."""
        try:
            # Check if it's a RAW file
            raw_extensions = {".arw", ".cr2", ".nef", ".dng", ".raw"}
            if image_path.suffix.lower() in raw_extensions:
                # Process RAW file - use postprocess for reliable results
                with rawpy.imread(str(image_path)) as raw:
                    # Process to RGB array (half size for speed)
                    rgb = raw.postprocess(use_camera_wb=True, half_size=True)
                    img = Image.fromarray(rgb)
            else:
                # Process regular image file (context manager releases the file handle)
                with Image.open(image_path) as opened:
                    img = opened.convert("RGB")

            # Convert to RGB if needed and resize for analysis
            if img.mode != "RGB":
                img = img.convert("RGB")

            # Resize if too large (save tokens/processing)
            max_size = 1024
            if max(img.size) > max_size:
                img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

            # Save to bytes and encode
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=95)
            return base64.b64encode(buffer.getvalue()).decode()
        except Exception as e:
            raise ValueError(f"Failed to process image {image_path}: {e}")

    def _create_prompt(self, existing_tags: List[str] = None) -> str:
        """Create contextual prompt with existing tags."""
        base_prompt = """Analyze this photograph and rate it 1-5 stars based on technical quality, composition, and subject interest.

Guidelines (be conservative):
- Rate 1: Trash - serious technical flaws, unusable
- Rate 2: OK - no clear defects but nothing special
- Rate 3: Worth looking twice - good enough to consider keeping
- Rate 4: Worth editing - has clear potential, merits post-processing
- Rate 5: Absolute masterpiece - exceptional in every way

Provide a detailed description of what you see in the image.

Be AGGRESSIVE with tagging - aim to include at least one tag per major category when appropriate (subject, lighting, composition, quality, etc.). Better to over-tag than under-tag.

CRITICAL: Maximize use of existing_tags_used and minimize new_tags_needed. Always prefer reusing an existing tag (even if slightly imperfect) over creating a new one. Only create new tags for genuinely unique concepts not covered by any existing tag.

Return JSON only, no other text:
{
    "rating": 4,
    "existing_tags_used": ["portrait", "natural_light"],
    "new_tags_needed": ["graduation"],
    "description": "A young woman in a black graduation cap and gown stands smiling in front of a university building. She holds her diploma proudly while warm afternoon sunlight creates a natural rim light around her silhouette.",
    "notes": "Well-composed graduation portrait with natural lighting",
    "confidence": 0.85
}"""

        if existing_tags:
            # Group tags for better context
            categories = self._categorize_tags(existing_tags)
            context = f"""
AVAILABLE TAGS (approved and pending - treat all as usable):
Subjects: {', '.join(categories.get('subjects', []))}
Lighting: {', '.join(categories.get('lighting', []))}
Composition: {', '.join(categories.get('composition', []))}
Quality: {', '.join(categories.get('quality', []))}
Other: {', '.join(categories.get('other', []))}

IMPORTANT TAGGING RULES:
- These tags include both approved and pending tags - use any that apply
- Be EXTREMELY aggressive with reusing existing tags - prefer existing over new
- ALWAYS check for similar/related concepts before suggesting new tags
- Only suggest new tags if NO existing tag covers the concept (even partially)
- Better to use an imperfect existing tag than create a new one
- Aim for comprehensive tagging covering subject, lighting, composition, and quality aspects

EXAMPLES OF TAG REUSE (prefer existing):
- If 'casual' exists, don't create 'informal' or 'relaxed'
- If 'group' exists, don't create 'multiple_people' or 'crowd'
- If 'blur' exists, don't create 'blurry' or 'out_of_focus'  
- If 'indoor' exists, don't create 'inside' or 'interior'
- If 'celebration' exists, don't create 'party' or 'festive'
- If 'portrait' exists, don't create 'headshot' or 'face'
"""
            return context + base_prompt

        return base_prompt

    def _categorize_tags(self, tags: List[str]) -> dict:
        """Simple tag categorization for better prompting."""
        categories = {
            "subjects": [],
            "lighting": [],
            "composition": [],
            "quality": [],
            "other": [],
        }

        for tag in tags:
            tag_lower = tag.lower()
            if any(
                word in tag_lower
                for word in ["person", "family", "pet", "landscape", "food", "portrait"]
            ):
                categories["subjects"].append(tag)
            elif any(
                word in tag_lower
                for word in ["light", "golden", "shadow", "bright", "dark"]
            ):
                categories["lighting"].append(tag)
            elif any(word in tag_lower for word in ["close", "wide", "macro", "depth"]):
                categories["composition"].append(tag)
            elif any(
                word in tag_lower for word in ["sharp", "blur", "noise", "exposed"]
            ):
                categories["quality"].append(tag)
            else:
                categories["other"].append(tag)

        return categories

    async def analyze_photo(
        self, image_path: Path, existing_tags: List[str] = None
    ) -> AIAnalysisResponse:
        """Analyze photo using OpenAI Vision API."""
        max_retries = 3
        # Back off between attempts: transient failures (e.g. a file still being
        # delivered by a sync tool like Syncthing) need time to resolve, and
        # re-reading the same partial file milliseconds later is pointless.
        retry_delays = [2, 8]
        last_error = None

        for attempt in range(max_retries):
            try:
                image_b64 = self._encode_image(image_path)
                prompt = self._create_prompt(existing_tags)

                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/jpeg;base64,{image_b64}"
                                    },
                                },
                            ],
                        }
                    ],
                    max_tokens=500,
                    temperature=0.1,
                )

                # Parse JSON response
                content = response.choices[0].message.content.strip()
                if content.startswith("```json"):
                    content = content.split("```json")[1].split("```")[0].strip()
                elif content.startswith("```"):
                    content = content.split("```")[1].split("```")[0].strip()

                result = json.loads(content)
                return AIAnalysisResponse(**result)

            except json.JSONDecodeError as e:
                last_error = (
                    f"Failed to parse AI response as JSON (attempt {attempt + 1}): {e}"
                )
            except Exception as e:
                last_error = f"AI analysis failed (attempt {attempt + 1}): {type(e).__name__}: {e}"

            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delays[min(attempt, len(retry_delays) - 1)])

        raise RuntimeError(f"All retry attempts failed. Last error: {last_error}")

    def health_check(self) -> bool:
        """Check if OpenAI service is available."""
        try:
            # Simple sync check
            import openai

            client = openai.OpenAI(api_key=self.client.api_key)
            client.models.list()
            return True
        except Exception:
            return False
