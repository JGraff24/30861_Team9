"""Operator CLI: setup -> run once -> report against a deployed tester, with lost-response recovery.

The operator token is read from TEAM9_TESTER_TOKEN, never from the command line, and never printed.
A run POST is never repeated: if its response is lost, the stored report is polled instead.
Optionally writes the exact report bytes (--output) and a sanitized run record (--record) with
counts, failures to classify, coverage and the 14-operation status.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

from app.config import AppEnvironment
from tester.config import TargetAuth, TesterSettings
from tester.suite.manifest import SuiteManifest
from tester.suite.records import run_record
from tester.target import CommunicationError, DeadlineExceeded, TargetClient

RUN_READ_TIMEOUT_SECONDS = 630
REPORT_POLL_SECONDS = 1.0
DEFAULT_MANIFEST = TesterSettings().fixture_manifest_path


def poll_report(client: TargetClient, session_id: str, deadline: float) -> httpx.Response:
    client.deadline = deadline
    while time.monotonic() < deadline:
        try:
            response = client.request(
                "GET",
                "/test/report",
                identity="test-admin",
                params={"session_id": session_id},
            )
            if response.status_code != 409:
                return response
        except CommunicationError:
            pass  # Only safe report GETs are retried, within the explicit deadline.
        time.sleep(min(REPORT_POLL_SECONDS, max(0, deadline - time.monotonic())))
    raise DeadlineExceeded("stored report polling deadline reached")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tester-url", required=True, help="tester base URL, e.g. http://127.0.0.1:8001"
    )
    parser.add_argument("--target-url", required=True, help="approved system base URL to test")
    parser.add_argument("--output", type=Path, help="write the exact stored report bytes here")
    parser.add_argument("--record", type=Path, help="write a sanitized run record JSON here")
    parser.add_argument(
        "--target-revision",
        default="unknown",
        help="target tag/SHA from pod documentation; never inferred (default: unknown)",
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    token = os.environ.get("TEAM9_TESTER_TOKEN")
    if not token:
        print("Set TEAM9_TESTER_TOKEN to the operator token.", file=sys.stderr)
        return 2
    manifest = SuiteManifest.model_validate_json(args.manifest.read_bytes())
    access = TargetAuth(userA=token, userB=token, reviewer=token, **{"test-admin": token})
    environment = (
        AppEnvironment.PRODUCTION
        if args.tester_url.startswith("https://")
        else AppEnvironment.DEVELOPMENT
    )
    with TargetClient(args.tester_url, access, environment, time.monotonic() + 60) as client:
        try:
            setup = client.request(
                "POST",
                "/test/setup",
                identity="test-admin",
                json={"target_url": args.target_url},
                timeout=60,
            )
        except (CommunicationError, DeadlineExceeded):
            print(
                "Setup response lost; do not repeat this setup POST. Create a new session.",
                file=sys.stderr,
            )
            return 1
        if setup.status_code != 201:
            print(f"setup failed: HTTP {setup.status_code}", file=sys.stderr)
            return 1
        session_id = setup.json()["session_id"]
        print(f"session {session_id} ready; running suite once")
        started = time.monotonic()
        client.deadline = started + RUN_READ_TIMEOUT_SECONDS
        try:
            run = client.request(
                "POST",
                "/test/run",
                identity="test-admin",
                json={"session_id": session_id},
                timeout=RUN_READ_TIMEOUT_SECONDS,
            )
            print(f"run returned HTTP {run.status_code} after {time.monotonic() - started:.1f} s")
        except (CommunicationError, DeadlineExceeded) as exc:
            print(f"run response lost ({type(exc).__name__}); polling stored report")
        try:
            report = poll_report(client, session_id, time.monotonic() + RUN_READ_TIMEOUT_SECONDS)
        except (CommunicationError, DeadlineExceeded):
            print(
                "Report polling deadline reached; retain the session ID for later recovery.",
                file=sys.stderr,
            )
            return 1
        if report.status_code != 200:
            print(f"report unavailable: HTTP {report.status_code}", file=sys.stderr)
            return 1
    if args.output:
        args.output.write_bytes(report.content)
    try:
        record = run_record(
            report.content,
            manifest,
            tester_url=args.tester_url,
            target_revision=args.target_revision,
        )
    except ValueError:
        print(
            "Stored report/manifest mismatch; supply the tester's saved plan version.",
            file=sys.stderr,
        )
        return 1
    if args.record:
        args.record.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", "utf-8")
    summary = {key: record[key] for key in ("session_id", "target_url", "counts", "coverage")}
    summary["not_passed"] = [
        {k: v for k, v in item.items() if k != "classification"} for item in record["not_passed"]
    ]
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 1 if record["counts"]["fail"] or record["counts"]["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
