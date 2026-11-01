"""Live HTTP/API restart and separate worker process using explicit test-only adapters."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx


def test_live_process_restart_and_worker(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    environment = {
        "PATH": os.defpath,
        "PYTHONPATH": str(root),
        "HOME": str(tmp_path),
        "METADATA_DB_PATH": str(tmp_path / "system.sqlite3"),
        "BLOB_ROOT": str(tmp_path / "sources"),
        "RULESET_VERSION": "1.0.0",  # This process injects the historical two-rule double.
        "ENGINE_FACTORY": "tests.unit.jackson_support:TestEngine",
        "RUNNER_FACTORY": "tests.unit.jackson_support:create_runner",
        "SYSTEM_TOKEN_HASHES_JSON": json.dumps(
            {
                __import__("hashlib").sha256(b"test-only-token").hexdigest(): {
                    "subject": "A",
                    "role": "user",
                },
            }
        ),
    }
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:create_app",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-access-log",
    ]

    def start() -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            command,
            cwd=tmp_path,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                assert process.poll() is None
                try:
                    if (
                        httpx.get(
                            f"http://127.0.0.1:{port}/api/v1/health", timeout=0.2, trust_env=False
                        ).status_code
                        == 200
                    ):
                        return process
                except httpx.TransportError:
                    pass
                time.sleep(0.05)
            raise TimeoutError("Live system startup timed out")
        except BaseException:
            process.terminate()
            process.wait(timeout=5)
            raise

    process = start()
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            timeout=5,
            trust_env=False,
            headers={"Authorization": "Bearer test-only-token"},
        ) as client:
            response = client.post("/api/v1/workbooks", files={"file": ("budget.xlsx", b"source")})
            assert response.status_code == 202
            ids = response.json()
            vp = f"/api/v1/workbooks/{ids['workbookId']}/versions/{ids['versionId']}"
            ap = f"{vp}/audits/{ids['auditId']}"
            assert client.get(ap).json()["status"] == "PROCESSING"
            process.terminate()
            process.wait(timeout=5)
            process = start()
            assert client.get(vp).json()["status"] == "PROCESSING"
            subprocess.run(
                [sys.executable, "-m", "app.jobs.worker", "--once"],
                env=environment,
                cwd=tmp_path,
                check=True,
                timeout=10,
                capture_output=True,
            )
            assert client.get(ap).json()["status"] == "AUDIT_COMPLETE"
            assert (
                client.get(vp).json()["sha256"]
                == __import__("hashlib").sha256(b"source").hexdigest()
            )
    finally:
        process.terminate()
        process.wait(timeout=5)
