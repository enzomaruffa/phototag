import json
from pathlib import Path

from phototag.stack_ops import export_stacks
from phototag.storage.state_db import PhotoStatus, ProcessingStateDB

from .test_stacks import manifest_dict

SEQUENCE_ID = "3F9A1C2E-8B1D-4C6A-9E0F-2A7B5C4D3E21"
FOLDER = "2026-10-31_140322_focus_stack_3f9a1c2e"


def setup(tmp_path: Path) -> ProcessingStateDB:
    db = ProcessingStateDB(storage_dir=tmp_path / ".phototag")
    db.import_sequence(
        SEQUENCE_ID, "focus_stack", json.dumps(manifest_dict(frames=2)), "s.json", "", 2
    )
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    members = []
    for position, stem in enumerate(["DSC01234", "DSC01235"], start=1):
        for suffix in (".ARW", ".JPG"):
            inbox_path = f"inbox/{stem}{suffix}"
            (outbox / f"{stem}{suffix}").write_bytes(stem.encode())
            db.add_photo(inbox_path, None)
            db.update_photo_status(
                inbox_path,
                PhotoStatus.PROCESSED,
                {"moved_to": str(outbox / f"{stem}{suffix}")},
            )
            members.append((inbox_path, position, position, 0))
    db.record_sequence_match(SEQUENCE_ID, 0.0, 2, members)
    return db


def test_export_copies_members_in_frame_order(tmp_path):
    db = setup(tmp_path)
    out = tmp_path / "export"

    summary = export_stacks(db, out, [])

    folder = out / FOLDER
    assert summary.exported == [folder]
    assert sorted(p.name for p in folder.iterdir()) == [
        "001_DSC01234.ARW",
        "001_DSC01234.JPG",
        "002_DSC01235.ARW",
        "002_DSC01235.JPG",
    ]
    # Copies, never moves
    assert (tmp_path / "outbox" / "DSC01234.ARW").exists()


def test_export_filters_raw_or_jpeg_and_kind(tmp_path):
    db = setup(tmp_path)

    export_stacks(db, tmp_path / "raw", [], raw=True)
    export_stacks(db, tmp_path / "jpeg", [], raw=False)
    other = export_stacks(db, tmp_path / "brackets", [], kind="exposure_bracket")

    assert [p.suffix for p in (tmp_path / "raw" / FOLDER).iterdir()] == [".ARW"] * 2
    assert [p.suffix for p in (tmp_path / "jpeg" / FOLDER).iterdir()] == [".JPG"] * 2
    assert other.exported == []


def test_existing_exports_are_skipped_unless_forced(tmp_path):
    db = setup(tmp_path)
    out = tmp_path / "export"
    export_stacks(db, out, [])
    (out / FOLDER / "001_DSC01234.JPG").unlink()

    again = export_stacks(db, out, [])
    forced = export_stacks(db, out, [], force=True)

    assert again.skipped == 1 and again.exported == []
    assert forced.exported == [out / FOLDER]
    assert (out / FOLDER / "001_DSC01234.JPG").exists()


def test_stale_location_falls_back_to_the_search_dirs(tmp_path):
    db = setup(tmp_path)
    elsewhere = tmp_path / "moved"
    (tmp_path / "outbox").rename(elsewhere)

    summary = export_stacks(db, tmp_path / "export", [elsewhere])

    assert summary.missing == []
    assert len(list((tmp_path / "export" / FOLDER).iterdir())) == 4
