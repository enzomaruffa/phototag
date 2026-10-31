"""OpenAI implementation of AI service."""

import asyncio
import base64
import io
import json
import os
from pathlib import Path
from typing import List, Optional

from openai import AsyncOpenAI
from PIL import Image
import rawpy

from .base import AIService
from ..dating import source_context
from ..media import RAW_EXTENSIONS
from ..models.ai import AIAnalysisResponse
from ..storage.exif import CaptureContext


BASE_PROMPT = """Analyze this photograph. Return a rating, a description, tags, any confidently named subjects, and any date printed in the frame.

RATING (1-5 stars, from technical quality, composition and subject interest; be conservative):
- 1: Trash - serious technical flaws, unusable
- 2: OK - no clear defects but nothing special
- 3: Worth looking twice - good enough to consider keeping
- 4: Worth editing - clear potential, merits post-processing
- 5: Absolute masterpiece - exceptional in every way

DESCRIPTION: two to four factual sentences about what is in the frame, the setting and the light. Use the proper names from named_subjects when there are any ("The Mona Lisa behind glass at the Louvre", not "a painting of a woman").

TAGS: lowercase snake_case vocabulary words for the subject, lighting, composition and quality. Tag generously - aim for at least one tag per category that applies; over-tagging beats under-tagging. Put a tag you took from AVAILABLE TAGS in existing_tags_used and only genuinely new concepts in new_tags_needed. Proper names never go in either list; they belong in named_subjects.

NAMED SUBJECTS: name specific, recognizable things when you are confident: artworks ("Mona Lisa" by Leonardo da Vinci), landmarks ("Eiffel Tower"), notable buildings and places, species ("Rufous hornero", not just "bird"), products and vehicle models ("Porsche 911 (992)"), and well-known events. kind is one of: artwork, landmark, building, place, species, product, vehicle, event, public_figure, other.
- Only include a subject at confidence 0.7 or higher. Never guess; when unsure, leave it out. An empty list is the normal answer.
- Never identify private individuals. Name a person only when they are a widely known public figure AND the scene makes it unambiguous (a named stage, a caption, a jersey); otherwise describe them without a name.
- CAPTURE CONTEXT, when given, can confirm a place or landmark but never replaces what is visible.

DATE STAMP: if a date is printed or burned into the image (corner digits are common on film and toy cameras), return it as visible_date in YYYY-MM-DD form, converting whatever format is shown. Only report a date you can clearly read; otherwise null. Never infer it from the scene or the capture context.

Return JSON only, no other text:
{
    "rating": 4,
    "existing_tags_used": ["architecture", "golden_hour"],
    "new_tags_needed": ["river"],
    "description": "The Eiffel Tower seen from across the Seine at sunset. Warm side light picks out the iron lattice while tour boats pass in the foreground.",
    "notes": "Classic composition; a slightly lower horizon would help",
    "confidence": 0.85,
    "visible_date": null,
    "named_subjects": [{"name": "Eiffel Tower", "kind": "landmark", "confidence": 0.98}]
}"""

TAG_REUSE_RULES = """Reuse these whenever they fit, even imperfectly; create a new tag only when no existing tag covers the concept, not even partially. Check for synonyms first:
- 'casual' exists -> don't create 'informal' or 'relaxed'
- 'group' exists -> don't create 'multiple_people' or 'crowd'
- 'blur' exists -> don't create 'blurry' or 'out_of_focus'
- 'indoor' exists -> don't create 'inside' or 'interior'
- 'celebration' exists -> don't create 'party' or 'festive'
- 'portrait' exists -> don't create 'headshot' or 'face'
"""


def capture_context_line(ctx: Optional[CaptureContext]) -> str:
    """Prompt line with the camera's GPS position and capture date, if known."""
    if ctx is None:
        return ""
    parts = []
    if ctx.latitude is not None and ctx.longitude is not None:
        parts.append(f"near {ctx.latitude:.4f}, {ctx.longitude:.4f}")
    if ctx.taken_on:
        parts.append(f"on {ctx.taken_on}")
    if not parts:
        return ""
    return f"CAPTURE CONTEXT (from the file's EXIF): Taken {' '.join(parts)}.\n"


class OpenAIService(AIService):
    """OpenAI implementation for photo analysis."""

    DEFAULT_MODEL = "gpt-6-luna"

    def __init__(self, api_key: str, model: Optional[str] = None):
        self.client = AsyncOpenAI(api_key=api_key)
        # OPENAI_MODEL env var overrides the default.
        self.model = model or os.getenv("OPENAI_MODEL", self.DEFAULT_MODEL)

    def _encode_image(self, image_path: Path) -> tuple:
        """Encode image to base64 for OpenAI; returns (b64, orig_width, orig_height)."""
        try:
            # Check if it's a RAW file
            if image_path.suffix.lower() in RAW_EXTENSIONS:
                # Process RAW file - use postprocess for reliable results
                with rawpy.imread(str(image_path)) as raw:
                    # Process to RGB array (half size for speed)
                    rgb = raw.postprocess(use_camera_wb=True, half_size=True)
                    img = Image.fromarray(rgb)
                    # half_size halved both dimensions; report the sensor's real size
                    orig_size = (img.size[0] * 2, img.size[1] * 2)
            else:
                # Process regular image file (context manager releases the file handle)
                with Image.open(image_path) as opened:
                    orig_size = opened.size
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
            return base64.b64encode(buffer.getvalue()).decode(), *orig_size
        except Exception as e:
            raise ValueError(f"Failed to process image {image_path}: {e}")

    def _create_prompt(
        self,
        existing_tags: Optional[List[str]] = None,
        source_ctx: str = "",
        capture_ctx: Optional[CaptureContext] = None,
        subject_hint: Optional[str] = None,
    ) -> str:
        """Prompt with source class, capture context and the tag vocabulary."""
        sections = [source_ctx + capture_context_line(capture_ctx)]
        if subject_hint:
            sections.append(
                f'PHOTOGRAPHER\'S NOTE: the subject is "{subject_hint}". These are '
                "the photographer's own words: use them to name the subject and keep "
                "them in the description, but do not invent details they do not "
                "support.\n"
            )

        if existing_tags:
            categories = self._categorize_tags(existing_tags)
            sections.append(
                f"""AVAILABLE TAGS (approved and pending - all usable):
Subjects: {", ".join(categories.get("subjects", []))}
Lighting: {", ".join(categories.get("lighting", []))}
Composition: {", ".join(categories.get("composition", []))}
Quality: {", ".join(categories.get("quality", []))}
Other: {", ".join(categories.get("other", []))}

{TAG_REUSE_RULES}"""
            )

        sections.append(BASE_PROMPT)
        return "\n".join(s for s in sections if s)

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
        self,
        image_path: Path,
        existing_tags: Optional[List[str]] = None,
        capture_ctx: Optional[CaptureContext] = None,
        subject_hint: Optional[str] = None,
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
                image_b64, orig_w, orig_h = self._encode_image(image_path)
                source_ctx = source_context(orig_w, orig_h, image_path.stat().st_size)
                prompt = self._create_prompt(
                    existing_tags, source_ctx, capture_ctx, subject_hint
                )

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
                    max_completion_tokens=2000,
                    # Guarantees syntactically valid JSON - no markdown fences,
                    # no parse-failure retries
                    response_format={"type": "json_object"},
                )

                result = json.loads(response.choices[0].message.content or "")
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
