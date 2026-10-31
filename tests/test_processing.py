import json
import subprocess
from pathlib import Path
from typing import List, Optional

from PIL import Image

from phototag.ai.openai_service import OpenAIService
from phototag.models.ai import AIAnalysisResponse
from phototag.processing.photo_processor import process_single_photo
from phototag.storage.exif import CaptureContext, EXIFHandler
from phototag.storage.state_db import PhotoStatus, ProcessingStateDB
from phototag.storage.tag_review import TagReviewStorage

from .test_stacks import manifest_dict

SEQUENCE_ID = "3F9A1C2E-8B1D-4C6A-9E0F-2A7B5C4D3E21"


class FakeAI(OpenAIService):
    """Counts calls instead of talking to OpenAI."""

    def __init__(self, fail: bool = False):
        self.calls: List[str] = []
        self.fail = fail

    async def analyze_photo(
        self,
        image_path: Path,
        existing_tags: Optional[List[str]] = None,
        capture_ctx: Optional[CaptureContext] = None,
    ) -> AIAnalysisResponse:
        self.calls.append(image_path.name)
        if self.fail:
            raise RuntimeError("model down")
        return AIAnalysisResponse.model_validate(
            {
                "rating": 4,
                "existing_tags_used": ["macro"],
                "new_tags_needed": ["nest"],
                "description": "A bird nest on a branch.",
                "named_subjects": [
                    {"name": "Rufous hornero", "kind": "species", "confidence": 0.9}
                ],
            }
        )


class Pipeline:
    def __init__(self, tmp_path: Path):
        self.db = ProcessingStateDB(storage_dir=tmp_path / ".phototag")
        self.tags = TagReviewStorage(storage_dir=tmp_path / ".phototag")
        self.tags.save_approved_tags([{"name": "macro"}])
        self.inbox = tmp_path / "inbox"
        self.inbox.mkdir()
        self.processed = tmp_path / "processed"
        self.processed.mkdir()
        self.exif = EXIFHandler()
        self.db.import_sequence(
            SEQUENCE_ID,
            "focus_stack",
            json.dumps(manifest_dict(frames=3)),
            "seq.json",
            "2026-10-31T14:03:22",
            3,
        )
        members = []
        for i in range(3):
            path = self.photo(f"DSC{i:05d}.JPG")
            members.append((str(path), i + 1, i + 1, int(i == 1)))
        self.db.record_sequence_match(SEQUENCE_ID, 37.0, 3, members)
        self.members = [m[0] for m in members]

    def photo(self, name: str) -> Path:
        path = self.inbox / name
        Image.new("RGB", (16, 16), "green").save(path)
        self.db.add_photo(str(path), None)
        return path

    def run(self, filepath: str, ai: FakeAI) -> PhotoStatus:
        return process_single_photo(
            Path(filepath),
            ai,
            self.exif,
            self.tags,
            self.db,
            self.processed,
            "test",
        )

    def keywords(self, filepath: str) -> List[str]:
        row = self.db.get_photo_status(filepath)
        assert row is not None
        out = subprocess.run(
            ["exiftool", "-j", "-Keywords", row["moved_to_path"]],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        keywords = json.loads(out)[0].get("Keywords", [])
        return [keywords] if isinstance(keywords, str) else keywords


def test_followers_reuse_the_representative_analysis(tmp_path):
    pipeline = Pipeline(tmp_path)
    ai = FakeAI()
    first, representative, last = pipeline.members

    assert pipeline.run(representative, ai) == PhotoStatus.PROCESSED
    assert pipeline.run(first, ai) == PhotoStatus.PROCESSED
    assert pipeline.run(last, ai) == PhotoStatus.PROCESSED

    assert ai.calls == ["DSC00001.JPG"]
    row = pipeline.db.get_photo_status(first)
    assert row is not None and row["ai_response_json"]
    assert json.loads(row["ai_response_json"])["rating"] == 4
    assert row["processed_sha1"]
    for member in pipeline.members:
        # 'nest' is still pending review; stack and named-subject tags skip it
        assert pipeline.keywords(member) == [
            "macro",
            "rufous_hornero",
            "focus_stack",
            "stack_3f9a1c2e",
        ]


def test_follower_is_analyzed_normally_when_the_representative_failed(tmp_path):
    pipeline = Pipeline(tmp_path)
    first, representative, _ = pipeline.members

    assert pipeline.run(representative, FakeAI(fail=True)) == PhotoStatus.FAILED
    ai = FakeAI()
    assert pipeline.run(first, ai) == PhotoStatus.PROCESSED

    assert ai.calls == ["DSC00000.JPG"]


def test_photos_outside_stacks_are_untouched(tmp_path):
    pipeline = Pipeline(tmp_path)
    loose = pipeline.photo("IMG_0001.JPG")

    assert pipeline.run(str(loose), FakeAI()) == PhotoStatus.PROCESSED
    assert pipeline.keywords(str(loose)) == ["macro", "rufous_hornero"]


def test_claim_defers_followers_until_the_second_wave(tmp_path):
    pipeline = Pipeline(tmp_path)
    loose = pipeline.photo("IMG_0001.JPG")

    claimed = []
    while path := pipeline.db.claim_next_photo("w", defer_stack_followers=True):
        claimed.append(path)
    later = []
    while path := pipeline.db.claim_next_photo("w"):
        later.append(path)

    assert sorted(claimed) == sorted([pipeline.members[1], str(loose)])
    assert sorted(later) == sorted([pipeline.members[0], pipeline.members[2]])
