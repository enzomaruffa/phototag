"""Receive Open Alpha Link sequence manifests over the local network.

`phototag receive` serves a small HTTP API on the LAN and advertises it over
Bonjour as `_phototag._tcp`, so the iPhone app can push each manifest straight
to the Mac. Writes need the pairing code from `.phototag/receiver.json`.
"""

import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional

import qrcode
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from rich.console import Console
from zeroconf import ServiceInfo, Zeroconf

from .stack_ops import RECEIVED_MANIFEST_DIR
from .stacks import MANIFEST_FORMAT, MANIFEST_VERSION, parse_manifest

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_phototag._tcp.local."
API_PATH = "/v1"
DEFAULT_PORT = 8765
CONFIG_PATH = Path(".phototag/receiver.json")
MAX_BODY_BYTES = 1024 * 1024

# No 0/O or 1/I, so a typed code can't be misread
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_GROUPS = 6
CODE_GROUP_LENGTH = 4


@dataclass
class ReceiverConfig:
    code: str

    @property
    def display_code(self) -> str:
        return "-".join(
            self.code[i : i + CODE_GROUP_LENGTH]
            for i in range(0, len(self.code), CODE_GROUP_LENGTH)
        )


def normalize_code(code: str) -> str:
    """Uppercase letters and digits only, so dashes and spaces don't matter."""
    return "".join(c for c in code.upper() if c.isascii() and c.isalnum())


def new_code() -> str:
    length = CODE_GROUPS * CODE_GROUP_LENGTH
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def load_or_create_config(path: Path = CONFIG_PATH) -> ReceiverConfig:
    """The pairing code, generated on first run and kept afterwards."""
    try:
        code = normalize_code(json.loads(path.read_text())["code"])
        if code:
            return ReceiverConfig(code=code)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    config = ReceiverConfig(code=new_code())
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps({"code": config.code}, indent=2).encode())
    os.chmod(path, 0o600)
    return config


def receive_port() -> int:
    try:
        return int(os.getenv("PHOTOTAG_RECEIVE_PORT") or DEFAULT_PORT)
    except ValueError:
        logger.warning("PHOTOTAG_RECEIVE_PORT is not a number, using %d", DEFAULT_PORT)
        return DEFAULT_PORT


def pair_url(code: str, port: int) -> str:
    return f"phototag://pair?code={code}&port={port}"


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _canonical_id(value: str) -> str:
    """The manifest id as the app writes it (uppercase UUID); 400 otherwise."""
    try:
        return str(uuid.UUID(value)).upper()
    except ValueError:
        raise HTTPException(status_code=400, detail="id is not a UUID") from None


def create_app(manifest_dir: Path, code: str) -> FastAPI:
    """The receiver API. Every route but /healthz needs `Authorization: Bearer <code>`."""
    expected = normalize_code(code).encode()
    app = FastAPI(title="phototag receiver", docs_url=None, redoc_url=None)

    def authorized(request: Request) -> None:
        scheme, _, presented = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            normalize_code(presented).encode(), expected
        ):
            raise HTTPException(status_code=401, detail="wrong or missing pairing code")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get(f"{API_PATH}/manifests", dependencies=[Depends(authorized)])
    def list_manifests() -> dict:
        items = []
        if manifest_dir.is_dir():
            for path in sorted(manifest_dir.glob("*.json")):
                with contextlib.suppress(OSError):
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    items.append({"id": path.stem, "sha256": digest})
        return {"manifests": items}

    @app.put(
        f"{API_PATH}/manifests/{{manifest_id}}", dependencies=[Depends(authorized)]
    )
    async def put_manifest(manifest_id: str, request: Request) -> JSONResponse:
        canonical = _canonical_id(manifest_id)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="manifest larger than 1 MB")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="manifest larger than 1 MB")
        data = bytes(body)

        try:
            document = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(status_code=400, detail="body is not JSON") from None
        if not isinstance(document, dict) or document.get("format") != MANIFEST_FORMAT:
            raise HTTPException(
                status_code=422, detail=f"format is not {MANIFEST_FORMAT}"
            )
        if document.get("version") != MANIFEST_VERSION:
            raise HTTPException(
                status_code=422, detail=f"version is not {MANIFEST_VERSION}"
            )
        if str(document.get("id", "")).upper() != canonical:
            raise HTTPException(status_code=422, detail="id does not match the path")
        if parse_manifest(document) is None:
            raise HTTPException(status_code=422, detail="not a valid sequence manifest")

        manifest_dir.mkdir(parents=True, exist_ok=True)
        path = manifest_dir / f"{canonical}.json"
        previous: Optional[bytes] = None
        with contextlib.suppress(FileNotFoundError):
            previous = path.read_bytes()
        if previous != data:
            _atomic_write(path, data)
        status = (
            "created"
            if previous is None
            else "unchanged"
            if previous == data
            else "updated"
        )
        logger.info("manifest %s %s", canonical, status)
        return JSONResponse(
            status_code=201 if previous is None else 200,
            content={
                "id": canonical,
                "status": status,
                "sha256": hashlib.sha256(data).hexdigest(),
            },
        )

    return app


def local_host_name() -> str:
    """The Bonjour host name the OS already answers for, so clients can resolve it.

    On macOS that is LocalHostName, which often differs from gethostname().
    """
    if sys.platform == "darwin":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            name = subprocess.run(
                ["scutil", "--get", "LocalHostName"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            ).stdout.strip()
            if name:
                return name
    return socket.gethostname().split(".")[0]


def service_name() -> str:
    return f"phototag on {local_host_name()}"


def lan_addresses() -> List[str]:
    """The IPv4 address of the interface that routes off the machine."""
    with contextlib.suppress(OSError):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            # UDP connect sends nothing; it only picks the outgoing interface
            probe.connect(("10.255.255.255", 1))
            return [probe.getsockname()[0]]
    return ["127.0.0.1"]


@contextlib.contextmanager
def advertise(port: int, name: Optional[str] = None) -> Iterator[ServiceInfo]:
    """Registers `_phototag._tcp` for as long as the block runs."""
    name = name or service_name()
    host = local_host_name()
    info = ServiceInfo(
        SERVICE_TYPE,
        f"{name}.{SERVICE_TYPE}",
        port=port,
        addresses=[socket.inet_aton(a) for a in lan_addresses()],
        properties={"v": "1", "path": API_PATH},
        server=f"{host}.local.",
    )
    zc = Zeroconf()
    try:
        zc.register_service(info, allow_name_change=True)
        yield info
    finally:
        with contextlib.suppress(Exception):
            zc.unregister_service(info)
        zc.close()


def print_pairing(console: Console, config: ReceiverConfig, port: int) -> None:
    url = pair_url(config.display_code, port)
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    console.print("📱 Scan with the iPhone Camera to pair Open Alpha Link:")
    qr.print_ascii(out=console.file, invert=True)
    console.print(
        f"   or type the code under Mac Sync: [bold]{config.display_code}[/bold]"
    )


def bind(port: int) -> socket.socket:
    """A listening socket on `port`, or on a free port when it is taken.

    The app finds the receiver over Bonjour, so any port works.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", port))
    except OSError:
        logger.warning("port %d is in use, picking a free one", port)
        sock.bind(("0.0.0.0", 0))
    sock.listen(64)
    return sock


class _Server(uvicorn.Server):
    """Stops on SIGINT/SIGTERM without re-raising the signal afterwards.

    uvicorn re-raises it once shut down, which would kill the process before
    `advertise` withdraws the Bonjour record and leave a stale receiver behind.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        stops = (signal.SIGINT, signal.SIGTERM)
        previous = {s: signal.signal(s, self.handle_exit) for s in stops}
        try:
            yield
        finally:
            for s, handler in previous.items():
                signal.signal(s, handler)


def serve(
    console: Console,
    port: int,
    manifest_dir: Path = RECEIVED_MANIFEST_DIR,
    config_path: Path = CONFIG_PATH,
    show_qr: bool = True,
) -> None:
    config = load_or_create_config(config_path)
    app = create_app(manifest_dir, config.code)
    sock = bind(port)
    port = sock.getsockname()[1]
    if show_qr:
        print_pairing(console, config, port)
    server = _Server(uvicorn.Config(app, log_level="warning"))
    with advertise(port) as info:
        console.print(
            f"📡 {info.name.removesuffix('.' + SERVICE_TYPE)} listening on"
            f" {', '.join(lan_addresses())}:{port}, saving to {manifest_dir.resolve()}"
        )
        server.run(sockets=[sock])
