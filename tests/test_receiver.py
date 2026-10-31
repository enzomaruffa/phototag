import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from phototag import receiver
from phototag.stack_ops import RECEIVED_MANIFEST_DIR, import_manifests, manifest_dirs
from phototag.storage.state_db import ProcessingStateDB

from .test_stacks import manifest_dict

CODE = "ABCDEFGHJKLMNPQRSTUVWXYZ"
ID = "3F9A1C2E-8B1D-4C6A-9E0F-2A7B5C4D3E21"
AUTH = {"Authorization": f"Bearer {CODE}"}


def body(**changes: Any) -> bytes:
    return json.dumps({**manifest_dict(), **changes}, indent=2).encode()


@pytest.fixture
def received(tmp_path: Path) -> Path:
    return tmp_path / "manifests"


@pytest.fixture
def client(received: Path) -> TestClient:
    return TestClient(receiver.create_app(received, CODE))


def test_first_put_creates_the_file(client: TestClient, received: Path):
    response = client.put(f"/v1/manifests/{ID}", content=body(), headers=AUTH)
    assert response.status_code == 201
    assert response.json()["status"] == "created"
    assert (received / f"{ID}.json").read_bytes() == body()


def test_repeat_put_is_unchanged_and_edit_is_updated(
    client: TestClient, received: Path
):
    client.put(f"/v1/manifests/{ID}", content=body(), headers=AUTH)
    same = client.put(f"/v1/manifests/{ID}", content=body(), headers=AUTH)
    assert (same.status_code, same.json()["status"]) == (200, "unchanged")

    edited = body(annotations={"title": "Hornero nest"})
    response = client.put(f"/v1/manifests/{ID}", content=edited, headers=AUTH)
    assert (response.status_code, response.json()["status"]) == (200, "updated")
    assert (received / f"{ID}.json").read_bytes() == edited


def test_list_reports_ids_with_digests(client: TestClient):
    assert client.get("/v1/manifests", headers=AUTH).json() == {"manifests": []}
    client.put(f"/v1/manifests/{ID}", content=body(), headers=AUTH)
    listing = client.get("/v1/manifests", headers=AUTH).json()
    assert listing == {
        "manifests": [{"id": ID, "sha256": hashlib.sha256(body()).hexdigest()}]
    }


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer WRONGCODE"}, {"Authorization": f"Basic {CODE}"}],
)
def test_writes_and_reads_need_the_pairing_code(
    client: TestClient, received: Path, headers
):
    assert (
        client.put(f"/v1/manifests/{ID}", content=body(), headers=headers).status_code
        == 401
    )
    assert client.get("/v1/manifests", headers=headers).status_code == 401
    assert not received.exists()


def test_code_comparison_ignores_dashes_and_case(client: TestClient):
    typed = {"Authorization": "Bearer abcd-efgh-jklm-npqr-stuv-wxyz"}
    assert (
        client.put(f"/v1/manifests/{ID}", content=body(), headers=typed).status_code
        == 201
    )


def test_healthz_needs_no_code(client: TestClient):
    assert client.get("/healthz").json() == {"ok": True}


@pytest.mark.parametrize(
    "payload, status",
    [
        (body(format="something.else"), 422),
        (body(version=2), 422),
        (body(id="00000000-0000-0000-0000-000000000000"), 422),
        (body(frames="nope"), 422),
        (b"not json", 400),
    ],
)
def test_rejects_anything_but_a_v1_manifest_for_this_id(
    client: TestClient, received: Path, payload: bytes, status: int
):
    response = client.put(f"/v1/manifests/{ID}", content=payload, headers=AUTH)
    assert response.status_code == status
    assert not (received / f"{ID}.json").exists()


def test_path_must_be_a_uuid(client: TestClient, received: Path):
    response = client.put(
        "/v1/manifests/..%2F..%2Fescape", content=body(), headers=AUTH
    )
    assert response.status_code in (400, 404)
    assert not received.exists()


def test_bodies_over_one_megabyte_are_refused(client: TestClient, received: Path):
    huge = body(annotations={"notes": "x" * receiver.MAX_BODY_BYTES})
    response = client.put(f"/v1/manifests/{ID}", content=huge, headers=AUTH)
    assert response.status_code == 413
    assert not (received / f"{ID}.json").exists()


def test_pairing_code_is_created_once_and_kept(tmp_path: Path):
    path = tmp_path / ".phototag" / "receiver.json"
    first = receiver.load_or_create_config(path)
    assert len(first.code) == 24
    assert set(first.code) <= set(receiver.CODE_ALPHABET)
    assert first.display_code.count("-") == 5
    assert receiver.load_or_create_config(path) == first
    assert path.stat().st_mode & 0o077 == 0


def test_pair_url_holds_code_and_port():
    assert (
        receiver.pair_url("ABCD-EFGH", 8765)
        == "phototag://pair?code=ABCD-EFGH&port=8765"
    )


def test_received_dir_is_always_a_manifest_dir(monkeypatch):
    monkeypatch.delenv("SEQUENCE_MANIFEST_DIR", raising=False)
    assert manifest_dirs() == [RECEIVED_MANIFEST_DIR]
    monkeypatch.setenv("SEQUENCE_MANIFEST_DIR", "/a, /b")
    assert manifest_dirs() == [RECEIVED_MANIFEST_DIR, Path("/a"), Path("/b")]


def test_received_manifests_are_imported(
    client: TestClient, received: Path, tmp_path: Path
):
    client.put(f"/v1/manifests/{ID}", content=body(), headers=AUTH)
    db = ProcessingStateDB(storage_dir=tmp_path / ".phototag")
    summary = import_manifests(db, [received])
    assert summary.imported == 1
    assert db.get_sequence(ID) is not None


class FakeZeroconf:
    instances: List["FakeZeroconf"] = []

    def __init__(self):
        self.registered: List[Any] = []
        self.unregistered: List[Any] = []
        self.closed = False
        FakeZeroconf.instances.append(self)

    def register_service(self, info, allow_name_change: bool = False):
        self.registered.append(info)

    def unregister_service(self, info):
        self.unregistered.append(info)

    def close(self):
        self.closed = True


def test_advertises_phototag_service_while_running(monkeypatch):
    FakeZeroconf.instances = []
    monkeypatch.setattr(receiver, "Zeroconf", FakeZeroconf)
    monkeypatch.setattr(receiver, "lan_addresses", lambda: ["192.168.1.20"])
    with receiver.advertise(8765, name="phototag on studio") as info:
        zc = FakeZeroconf.instances[0]
        assert zc.registered == [info]
        assert info.type == "_phototag._tcp.local."
        assert info.name == "phototag on studio._phototag._tcp.local."
        assert info.port == 8765
        assert info.parsed_addresses() == ["192.168.1.20"]
        properties: Dict[bytes, Any] = info.properties
        assert properties == {b"v": b"1", b"path": b"/v1"}
    assert zc.unregistered == [info]
    assert zc.closed


def test_bind_falls_back_to_a_free_port_when_taken():
    taken = receiver.bind(0)
    try:
        port = taken.getsockname()[1]
        fallback = receiver.bind(port)
        try:
            assert fallback.getsockname()[1] not in (0, port)
        finally:
            fallback.close()
    finally:
        taken.close()
