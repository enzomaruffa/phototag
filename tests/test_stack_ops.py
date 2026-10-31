import json
from pathlib import Path
from typing import Any, Dict, List

from phototag.stack_ops import import_manifests, match_inbox
from phototag.storage.exif import EXIFHandler
from phototag.storage.state_db import ProcessingStateDB

from .test_stacks import manifest_dict


class FakeExif(EXIFHandler):
    """Answers the batched capture-time read from a dict instead of exiftool."""

    def __init__(self, records: Dict[str, Dict[str, Any]]):
        self.records = records
        self.calls = 0

    def read_shot_metadata(self, paths: List[Path]) -> List[Dict[str, Any]]:
        self.calls += 1
        return [
            {"SourceFile": str(p), **self.records[p.name]}
            for p in paths
            if p.name in self.records
        ]


def setup(tmp_path: Path):
    db = ProcessingStateDB(storage_dir=tmp_path / ".phototag")
    inbox = tmp_path / "inbox"
    (inbox / "card").mkdir(parents=True)
    records = {}
    # Camera clock 37 s behind the phone: frame i pressed at 14:03:22 + 3i local
    for i in range(5):
        for suffix in (".ARW", ".JPG"):
            name = f"DSC{100 + i:05d}{suffix}"
            (inbox / "card" / name).write_bytes(b"x")
            second = 22 + 3 * i - 37
            records[name] = {
                "DateTimeOriginal": f"2026:10:31 14:{2 + (second + 60) // 60:02d}:{(second + 60) % 60:02d}",
                "Model": "ILCE-7M3",
            }
    (inbox / "card" / "DSC09999.JPG").write_bytes(b"x")
    records["DSC09999.JPG"] = {
        "DateTimeOriginal": "2026:10:31 18:00:00",
        "Model": "ILCE-7M3",
    }
    (inbox / "seq.json").write_text(json.dumps(manifest_dict()))
    (inbox / "unrelated.json").write_text(json.dumps({"hello": "world"}))
    (inbox / "broken.json").write_text("{")
    return db, inbox, FakeExif(records)


def test_import_and_match_inbox(tmp_path):
    db, inbox, exif = setup(tmp_path)

    summary = import_manifests(db, [tmp_path / "missing-icloud-dir", inbox])
    match_inbox(db, inbox, 120, exif=exif, summary=summary)

    assert summary.imported == 1
    assert summary.matched == ["3F9A1C2E-8B1D-4C6A-9E0F-2A7B5C4D3E21"]
    assert exif.calls == 1
    [sequence] = db.get_sequences()
    assert sequence["status"] == "matched"
    assert abs(sequence["matched_offset_seconds"] - 37) <= 1.5
    members = db.get_stack_members(sequence["id"])
    names = [Path(m["filepath"]).name for m in members]
    assert "DSC09999.JPG" not in names
    assert len(names) == 10
    assert [m["position"] for m in members][:2] == [1, 1]
    [rep] = [m for m in members if m["is_representative"]]
    assert Path(rep["filepath"]).name == "DSC00102.JPG"
    # Member keys are the inbox paths the processor will use
    assert rep["filepath"] == str(inbox / "card" / "DSC00102.JPG")


def test_reimporting_the_same_manifest_is_a_no_op(tmp_path):
    db, inbox, exif = setup(tmp_path)
    import_manifests(db, [inbox])
    match_inbox(db, inbox, 120, exif=exif)

    again = import_manifests(db, [inbox])
    match_inbox(db, inbox, 120, exif=exif, summary=again)

    assert again.imported == 0 and again.already_known == 1
    assert again.matched == [] and again.unmatched == []
    assert exif.calls == 1  # nothing left to match, so no exiftool call


def test_sequence_without_photos_stays_unmatched_until_they_arrive(tmp_path):
    db, inbox, exif = setup(tmp_path)
    empty_inbox = tmp_path / "empty"
    empty_inbox.mkdir()
    import_manifests(db, [inbox])

    first = match_inbox(db, empty_inbox, 120, exif=exif)
    second = match_inbox(db, inbox, 120, exif=exif)

    assert len(first.unmatched) == 1
    assert len(second.matched) == 1


def test_schema_migration_is_idempotent(tmp_path):
    ProcessingStateDB(storage_dir=tmp_path)
    db = ProcessingStateDB(storage_dir=tmp_path)

    assert db.get_sequences() == []
