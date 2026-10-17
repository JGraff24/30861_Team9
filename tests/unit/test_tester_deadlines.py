"""Actual loopback sockets verify total deadlines against continually streaming responses."""

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.config import AppEnvironment
from app.contracts.models import TesterSetupRequest as SetupRequest
from tester.config import TargetAuth
from tester.config import TesterSettings as Settings
from tester.errors import TesterError as EndpointError
from tester.service import TesterService as Service
from tester.store import TesterSessionState as State
from tester.target import CommunicationError, DeadlineExceeded, TargetClient
from tests.support.fake_system import TOKENS

MANIFEST = Path(__file__).resolve().parents[1] / "fixtures" / "manifest.json"


@pytest.fixture
def streaming_target() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_bytes(json.dumps({"session_id": request["session_id"]}).encode())

        def do_GET(self) -> None:
            self.send_bytes(b"x" * 100)

        def send_bytes(self, data: bytes) -> None:
            self.send_response(201 if self.command == "POST" else 200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                for byte in data:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.01)  # Below the inactivity timeout, beyond total budget.
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(1)


def test_streamed_setup_cannot_become_ready_after_deadline(
    streaming_target: str, tmp_path: Path
) -> None:
    settings = Settings(
        _env_file=None,
        tester_db_path=tmp_path / "tester.sqlite3",
        fixture_manifest_path=MANIFEST,
        allowed_target_urls=[streaming_target],
        TARGET_AUTH_MAP_JSON={streaming_target: TOKENS},
        tester_setup_timeout_seconds=0.2,
    )
    service = Service(settings)
    started = time.monotonic()
    with pytest.raises(EndpointError) as caught:
        service.setup(SetupRequest(target_url=streaming_target), "operator")
    assert caught.value.status_code == 500 and caught.value.source == "communication"
    assert time.monotonic() - started < 0.8
    failed = service.store.cleanup_candidates(streaming_target, failed_only=True)
    assert len(failed) == 1 and service.store.get(failed[0]).state == State.SETUP_FAILED


def test_continuous_get_is_bounded_by_individual_call_budget(streaming_target: str) -> None:
    client = TargetClient(
        streaming_target, TargetAuth(**TOKENS), AppEnvironment.DEVELOPMENT, time.monotonic() + 5
    )
    started = time.monotonic()
    with pytest.raises(CommunicationError, match="deadline"):
        client.request("GET", "/slow", timeout=0.2)
    assert time.monotonic() - started < 0.8


def test_continuous_get_expires_whole_suite_budget(streaming_target: str) -> None:
    client = TargetClient(
        streaming_target, TargetAuth(**TOKENS), AppEnvironment.DEVELOPMENT, time.monotonic() + 0.2
    )
    with pytest.raises(DeadlineExceeded):
        client.request("GET", "/slow", timeout=1)
    with pytest.raises(DeadlineExceeded):
        client.request("POST", "/must-not-send", json={})
