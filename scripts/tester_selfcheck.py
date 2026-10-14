"""Local live self-check: real tester process vs. the TEST-ONLY fake target over real sockets.

This proves the tester's HTTP behavior end to end. It is not evidence about the Team 9 system,
AWS or Team 12. Starts its own processes on 8000/8001 and stops only those.
"""

import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from tests.support.fake_system import BASE_URL, access_map

REPO = Path(__file__).resolve().parents[1]


def wait_ready(port: int, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"process on {port} exited early")
        try:
            httpx.get(f"http://127.0.0.1:{port}/docs", timeout=1, trust_env=False)
            return
        except httpx.TransportError:
            time.sleep(0.2)
    raise TimeoutError(f"port {port} did not start")


def main() -> int:
    for port in (8000, 8001):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))  # refuse occupied ports
    token = secrets.token_urlsafe(24)
    with tempfile.TemporaryDirectory() as state:
        env = os.environ | {
            "TESTER_DB_PATH": str(Path(state) / "tester.sqlite3"),
            "TESTER_OPERATOR_TOKEN_HASHES_JSON": json.dumps(
                {hashlib.sha256(token.encode()).hexdigest(): "selfcheck-operator"}
            ),
            "ALLOWED_TARGET_URLS": json.dumps([BASE_URL]),
            "TARGET_AUTH_MAP_JSON": json.dumps(access_map()),
            "FIXTURE_MANIFEST_PATH": str(REPO / "tests" / "fixtures" / "manifest.json"),
        }
        commands = [
            ("tests.support.fake_target_app:app", 8000, []),
            ("tester.main:create_app", 8001, ["--factory"]),
        ]
        processes = []
        try:
            for module, port, extra in commands:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        module,
                        *extra,
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--log-level",
                        "warning",
                    ],
                    cwd=REPO,
                    env=env,
                )
                processes.append(process)
                wait_ready(port, process)
            child = os.environ | {
                "TEAM9_TESTER_TOKEN": token,
                "TEAM9_TESTER_URL": "http://127.0.0.1:8001",
                "TEAM9_TARGET_URL": BASE_URL,
            }
            report_path = Path(state) / "report.json"
            suite = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.run_suite",
                    "--tester-url",
                    "http://127.0.0.1:8001",
                    "--target-url",
                    BASE_URL,
                    "--output",
                    str(report_path),
                ],
                cwd=REPO,
                env=child,
            )
            acceptance = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "tests/acceptance",
                ],
                cwd=REPO,
                env=child,
            )
            return suite.returncode or acceptance.returncode
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
