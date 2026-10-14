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

from tester.suite.manifest import SuiteManifest
from tester.suite.records import run_record

RUN_READ_TIMEOUT_SECONDS = 630
REPORT_POLL_SECONDS = 1.0
DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "manifest.json"


def poll_report(client: httpx.Client, session_id: str, deadline: float) -> httpx.Response:
    while True:
        response = client.get("/test/report", params={"session_id": session_id})
        if response.status_code != 409 or time.monotonic() > deadline:
            return response
        time.sleep(REPORT_POLL_SECONDS)


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
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(
        base_url=args.tester_url.rstrip("/"), headers=headers, timeout=60, trust_env=False
    ) as client:
        setup = client.post("/test/setup", json={"target_url": args.target_url})
        if setup.status_code != 201:
            print(f"setup failed: HTTP {setup.status_code} {setup.text}", file=sys.stderr)
            return 1
        session_id = setup.json()["session_id"]
        print(f"session {session_id} ready; running suite once")
        started = time.monotonic()
        try:
            run = client.post(
                "/test/run",
                json={"session_id": session_id},
                timeout=httpx.Timeout(60, read=RUN_READ_TIMEOUT_SECONDS),
            )
            print(f"run returned HTTP {run.status_code} after {time.monotonic() - started:.1f} s")
        except httpx.TransportError as exc:
            print(f"run response lost ({type(exc).__name__}); polling stored report")
        report = poll_report(client, session_id, time.monotonic() + RUN_READ_TIMEOUT_SECONDS)
        if report.status_code != 200:
            print(f"report unavailable: HTTP {report.status_code} {report.text}", file=sys.stderr)
            return 1
    if args.output:
        args.output.write_bytes(report.content)
    record = run_record(
        report.content, manifest, tester_url=args.tester_url, target_revision=args.target_revision
    )
    if args.record:
        args.record.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", "utf-8")
    summary = {key: record[key] for key in ("session_id", "target_url", "counts", "coverage")}
    summary["not_passed"] = [
        {k: v for k, v in item.items() if k != "classification"} for item in record["not_passed"]
    ]
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
