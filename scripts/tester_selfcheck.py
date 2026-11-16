"""Local live self-check: real tester process vs. the TEST-ONLY fake target over real sockets.

This proves the tester's HTTP behavior end to end. It is not evidence about the Team 9 system,
AWS or Team 12. Starts its own processes on 8000/8001 and stops only those.
"""

import argparse
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

from tests.support.fake_phase5 import access_map as phase5_access_map
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-dir", type=Path, help="retain sanitized fake-target report/record"
    )
    parser.add_argument(
        "--phase4", action="store_true", help="run the Phase 4 manifest against the Phase 4 fake"
    )
    parser.add_argument(
        "--phase5", action="store_true", help="run the Phase 5 manifest against the Phase 5 fake"
    )
    parser.add_argument(
        "--profile",
        choices=("own", "cross"),
        default="own",
        help="Phase 5 trusted classification of the fake target (own-team or cross-team)",
    )
    args = parser.parse_args()
    if args.phase4 and args.phase5:
        parser.error("choose one of --phase4 or --phase5")
    name = (
        "manifest-phase5.json"
        if args.phase5
        else "manifest-phase4.json"
        if args.phase4
        else "manifest.json"
    )
    manifest = REPO / "tests" / "fixtures" / name
    phase = "5" if args.phase5 else "4" if args.phase4 else "3"
    read_timeout = {"5": "2430", "4": "1830", "3": "630"}[phase]
    if args.evidence_dir:
        args.evidence_dir.mkdir(parents=True, exist_ok=True)
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
            "TARGET_AUTH_MAP_JSON": json.dumps(
                phase5_access_map() if args.phase5 else access_map()
            ),
            "FIXTURE_MANIFEST_PATH": str(manifest),
            "FAKE_TARGET_PHASE": phase,
            # Trusted Phase 5 profile configuration (never part of a grader request).
            "TESTER_TEAM_ID": "team9",
            "TARGET_TEAM_MAP_JSON": json.dumps(
                {BASE_URL: "team9" if args.profile == "own" else "fake-cross-team"}
            ),
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
                "FIXTURE_MANIFEST_PATH": str(manifest),
                "TEAM9_RUN_READ_TIMEOUT": read_timeout,
                **({"TEAM9_EXPECT_PROFILE": args.profile} if args.phase5 else {}),
            }
            report_path = (args.evidence_dir or Path(state)) / "report.json"
            record_path = (args.evidence_dir or Path(state)) / "run-record.json"
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
                    "--record",
                    str(record_path),
                    "--manifest",
                    str(manifest),
                ],
                cwd=REPO,
                env=child,
            )
            if report_path.exists():
                report = json.loads(report_path.read_bytes())
                assert not any(r["outcome"] in ("fail", "error") for r in report["results"])
                if args.phase5:
                    assert ("model" in report["coverage"]) == (args.profile == "own")
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
