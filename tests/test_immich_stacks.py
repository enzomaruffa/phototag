import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

import phototag.storage.immich as immich
from phototag.stack_ops import link_immich_stacks
from phototag.storage.immich import ImmichUploader
from phototag.storage.state_db import PhotoStatus, ProcessingStateDB

from .test_stacks import manifest_dict

SEQUENCE_ID = "3F9A1C2E-8B1D-4C6A-9E0F-2A7B5C4D3E21"


class Response:
    def __init__(self, payload: Any, status: int = 200):
        self.payload = payload
        self.status_code = status

    def json(self) -> Any:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeImmich:
    """Stands in for the requests module: an Immich server holding some checksums."""

    def __init__(self, known: Dict[str, str], stack_status: int = 200):
        self.known = known
        self.stack_status = stack_status
        self.albums: List[Dict[str, Any]] = []
        self.calls: List[Tuple[str, str, Optional[Dict]]] = []

    def post(self, url: str, headers: Dict, json: Dict, timeout: int) -> Response:
        path = url.split("/api", 1)[1]
        self.calls.append(("POST", path, json))
        assert headers == {"x-api-key": "key"}
        if path == "/assets/bulk-upload-check":
            return Response(
                {
                    "results": [
                        {
                            "id": a["id"],
                            "action": "reject",
                            "reason": "duplicate",
                            "assetId": self.known[a["checksum"]],
                        }
                        if a["checksum"] in self.known
                        else {"id": a["id"], "action": "accept"}
                        for a in json["assets"]
                    ]
                }
            )
        if path == "/stacks":
            return Response({"id": "stack-1"}, self.stack_status)
        if path == "/albums":
            self.albums.append({"id": "album-1", "albumName": json["albumName"]})
            return Response(self.albums[-1])
        raise AssertionError(path)

    def put(self, url: str, headers: Dict, json: Dict, timeout: int) -> Response:
        self.calls.append(("PUT", url.split("/api", 1)[1], json))
        return Response([])

    def get(self, url: str, headers: Dict, timeout: int) -> Response:
        self.calls.append(("GET", url.split("/api", 1)[1], None))
        return Response(self.albums)


def setup_db(tmp_path: Path, statuses: List[str], album: Optional[str] = None):
    db = ProcessingStateDB(storage_dir=tmp_path)
    manifest = {**manifest_dict(frames=3), "annotations": {"album": album}}
    db.import_sequence(
        SEQUENCE_ID, "focus_stack", json.dumps(manifest), "s.json", "", 3
    )
    members = []
    for i, status in enumerate(statuses):
        path = f"inbox/DSC{i}.JPG"
        db.add_photo(path, None)
        db.update_photo_status(
            path,
            PhotoStatus(status),
            {"processed_hashes": (f"sha256-{i}", f"sha1-{i}")},
        )
        members.append((path, i + 1, i + 1, int(i == 1)))
    db.record_sequence_match(SEQUENCE_ID, 0.0, 3, members)
    return db


@pytest.fixture
def uploader() -> ImmichUploader:
    return ImmichUploader(server_host="immich.test", server_user="me")


def test_uploaded_sequence_becomes_a_stack_with_the_representative_first(
    tmp_path, monkeypatch, uploader
):
    db = setup_db(tmp_path, ["processed"] * 3)
    server = FakeImmich({"sha1-0": "a0", "sha1-1": "a1", "sha1-2": "a2"})
    monkeypatch.setattr(immich, "requests", server)

    summary = link_immich_stacks(db, uploader, "key")

    assert summary.stacked == 1 and summary.errors == []
    assert ("POST", "/stacks", {"assetIds": ["a1", "a0", "a2"]}) in server.calls
    assert db.get_sequences()[0]["immich_stack_id"] == "stack-1"

    server.calls.clear()
    assert link_immich_stacks(db, uploader, "key").stacked == 0
    assert server.calls == []  # already stacked, nothing to ask the server


def test_sequence_waits_until_every_member_is_on_the_server(
    tmp_path, monkeypatch, uploader
):
    db = setup_db(tmp_path, ["processed", "processed", "pending"])
    server = FakeImmich({"sha1-0": "a0", "sha1-1": "a1"})
    monkeypatch.setattr(immich, "requests", server)

    assert link_immich_stacks(db, uploader, "key").waiting == 1
    assert server.calls == []

    db.update_photo_status("inbox/DSC2.JPG", PhotoStatus.PROCESSED)
    summary = link_immich_stacks(db, uploader, "key")

    assert summary.waiting == 1 and summary.stacked == 0  # sha1-2 not uploaded


def test_stack_failure_is_reported_not_raised(tmp_path, monkeypatch, uploader):
    db = setup_db(tmp_path, ["processed"] * 3)
    monkeypatch.setattr(
        immich,
        "requests",
        FakeImmich({"sha1-0": "a0", "sha1-1": "a1", "sha1-2": "a2"}, stack_status=500),
    )

    summary = link_immich_stacks(db, uploader, "key")

    assert summary.stacked == 0
    assert summary.errors == ["3f9a1c2e: HTTP 500"]
    assert db.get_sequences()[0]["immich_stack_id"] is None


def test_annotation_album_is_created_then_reused(tmp_path, monkeypatch, uploader):
    db = setup_db(tmp_path, ["processed"] * 3, album="Macro 2026")
    server = FakeImmich({"sha1-0": "a0", "sha1-1": "a1", "sha1-2": "a2"})
    server.albums.append({"id": "album-9", "albumName": "Macro 2026"})
    monkeypatch.setattr(immich, "requests", server)

    summary = link_immich_stacks(db, uploader, "key")

    assert summary.albums == 1
    assert (
        "PUT",
        "/albums/album-9/assets",
        {"ids": ["a1", "a0", "a2"]},
    ) in server.calls
    assert db.get_sequences()[0]["immich_album"] == "Macro 2026"


def test_explicit_upload_album_overrides_the_annotation(
    tmp_path, monkeypatch, uploader
):
    db = setup_db(tmp_path, ["processed"] * 3, album="Macro 2026")
    server = FakeImmich({"sha1-0": "a0", "sha1-1": "a1", "sha1-2": "a2"})
    monkeypatch.setattr(immich, "requests", server)

    summary = link_immich_stacks(db, uploader, "key", album_override="Trip")

    assert summary.albums == 0 and summary.stacked == 1
    assert not any(path.startswith("/albums") for _, path, _ in server.calls)


def test_failed_members_do_not_block_the_stack(tmp_path, monkeypatch, uploader):
    db = setup_db(tmp_path, ["processed", "processed", "failed"])
    server = FakeImmich({"sha1-0": "a0", "sha1-1": "a1"})
    monkeypatch.setattr(immich, "requests", server)

    assert link_immich_stacks(db, uploader, "key").stacked == 1
    assert ("POST", "/stacks", {"assetIds": ["a1", "a0"]}) in server.calls


def test_record_new_location_follows_the_upload_move(tmp_path):
    db = ProcessingStateDB(storage_dir=tmp_path)
    db.add_photo("inbox/a.JPG", None)
    db.update_photo_status(
        "inbox/a.JPG", PhotoStatus.PROCESSED, {"moved_to": "processed/a.JPG"}
    )

    db.record_new_location("processed/a.JPG", "outbox/a.JPG")

    row = db.get_photo_status("inbox/a.JPG")
    assert row is not None and row["moved_to_path"] == "outbox/a.JPG"


def add_merged(db: ProcessingStateDB, status: str) -> None:
    path = "inbox/merged.jpg"
    db.add_photo(path, None)
    db.update_photo_status(
        path, PhotoStatus(status), {"processed_hashes": ("sha256-m", "sha1-m")}
    )
    db.set_sequence_merged(SEQUENCE_ID, "merged/merged.jpg", path)


def test_merged_image_becomes_the_stack_primary(tmp_path, monkeypatch, uploader):
    db = setup_db(tmp_path, ["processed"] * 3)
    add_merged(db, "processed")
    server = FakeImmich({"sha1-0": "a0", "sha1-1": "a1", "sha1-2": "a2", "sha1-m": "m"})
    monkeypatch.setattr(immich, "requests", server)

    assert link_immich_stacks(db, uploader, "key").stacked == 1
    assert ("POST", "/stacks", {"assetIds": ["m", "a1", "a0", "a2"]}) in server.calls

    server.calls.clear()
    assert link_immich_stacks(db, uploader, "key").stacked == 0
    assert server.calls == []


def test_stack_waits_for_a_merged_image_still_in_the_pipeline(
    tmp_path, monkeypatch, uploader
):
    db = setup_db(tmp_path, ["processed"] * 3)
    add_merged(db, "pending")
    server = FakeImmich({"sha1-0": "a0", "sha1-1": "a1", "sha1-2": "a2"})
    monkeypatch.setattr(immich, "requests", server)

    assert link_immich_stacks(db, uploader, "key").waiting == 1
    assert server.calls == []


def test_merged_image_uploaded_later_takes_over_the_stack(
    tmp_path, monkeypatch, uploader
):
    db = setup_db(tmp_path, ["processed"] * 3, album="Macro 2026")
    server = FakeImmich({"sha1-0": "a0", "sha1-1": "a1", "sha1-2": "a2", "sha1-m": "m"})
    server.albums.append({"id": "album-9", "albumName": "Macro 2026"})
    monkeypatch.setattr(immich, "requests", server)
    link_immich_stacks(db, uploader, "key")
    server.calls.clear()

    add_merged(db, "processed")
    summary = link_immich_stacks(db, uploader, "key")

    assert summary.stacked == 1 and summary.albums == 1
    assert ("POST", "/stacks", {"assetIds": ["m", "a1", "a0", "a2"]}) in server.calls
    assert db.get_sequences()[0]["immich_stack_primary"] == "sha1-m"
