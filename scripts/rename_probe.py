"""Probe one target with single-reference rename variants (worksheet-rename investigation).

Uses the tester's own trusted access configuration (ALLOWED_TARGET_URLS and
TARGET_AUTH_MAP_JSON from the environment), creates one fresh test namespace through normal
system setup, uploads each variant from tester/phase5/rename_probe.py, records the
WORKSHEET_NAME classification/target/finding ID, applies the rename when it is offered, and
compares the output with the tester's independent package comparator. The namespace is cleaned
through a cleanup carrier afterwards. Prints and optionally writes a JSON diagnostic; never
prints credentials. `--local-team9` starts the real Team 9 API and worker on private
temporary state instead of contacting any deployment.

This is investigation evidence only; it is not a suite run and it changes no expectation.
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
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import httpx

from tester.config import TesterSettings
from tester.phase4 import contract
from tester.phase4.package import PackageError, compare_packages
from tester.phase5.rename_probe import NEW_NAME, TARGET, expected_finding, plan, variants
from tester.suite.identity import finding_id
from tester.target import TargetClient

REPO = Path(__file__).resolve().parents[1]
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def poll(client: TargetClient, path: str, session: str, done: str, seconds: float) -> Any:
    deadline = time.monotonic() + seconds
    while True:
        response = client.request(
            "GET",
            path,
            identity="userA",
            session_header=False,
            headers={"X-Test-Session": session},
            timeout=10,
        )
        if response.status_code != 200:
            return {"http_status": response.status_code}
        body = response.json()
        if body["status"] != done or time.monotonic() >= deadline:
            return body
        time.sleep(0.5)


def probe(client: TargetClient, ruleset: str = "2.0.0") -> dict[str, Any]:
    session = f"t9-rename-probe-{uuid.uuid4().hex[:16]}"
    setup = client.request(
        "POST",
        "/test/setup",
        identity="test-admin",
        json={"session_id": session, "setup": {}},
        timeout=30,
    )
    if setup.status_code != 201:
        return {"error": f"system setup returned HTTP {setup.status_code}"}
    headers = {"X-Test-Session": session}
    results = []
    try:
        for variant in variants():
            row: dict[str, Any] = {
                "variant": variant.name,
                "description": variant.description,
                "sha256": hashlib.sha256(variant.data).hexdigest(),
                "interpretation_question": variant.interpretation_question,
            }
            results.append(row)
            upload = client.request(
                "POST",
                "/api/v1/workbooks",
                identity="userA",
                session_header=False,
                headers=headers,
                files={"file": (f"{variant.name}.xlsx", variant.data, XLSX)},
                timeout=35,
            )
            row["upload_status"] = upload.status_code
            if upload.status_code != 202:
                continue
            ids = upload.json()
            base = f"/api/v1/workbooks/{ids['workbookId']}/versions/{ids['versionId']}"
            audit = poll(client, f"{base}/audits/{ids['auditId']}", session, "PROCESSING", 80)
            row["audit_status"] = audit.get("status", audit)
            names = [f for f in audit.get("findings", []) if f["ruleId"] == "WORKSHEET_NAME"]
            row["worksheet_name_findings"] = [
                {k: f[k] for k in ("worksheet", "target", "classification")} for f in names
            ]
            row["other_findings"] = sorted(
                {f["ruleId"] for f in audit.get("findings", [])} - {"WORKSHEET_NAME"}
            )
            expected = expected_finding()
            wanted_id = finding_id(row["sha256"], ruleset, "WORKSHEET_NAME", "Sheet1", TARGET)
            match = next((f for f in names if f["target"] == TARGET), None)
            row["target_matches"] = match is not None
            row["finding_id_matches"] = bool(match and match["findingId"] == wanted_id)
            row["classification_matches"] = bool(
                match and match["classification"] == expected["classification"]
            )
            if not match or match["classification"] != "REQUIRES_USER_INPUT":
                continue
            submit = client.request(
                "POST",
                f"{base}/remediations",
                identity="userA",
                session_header=False,
                headers={**headers, "Idempotency-Key": f"t9-{uuid.uuid4().hex}"},
                json={
                    "auditId": ids["auditId"],
                    "decisions": [
                        {
                            "findingId": match["findingId"],
                            "action": "APPLY",
                            "value": {"name": NEW_NAME},
                        }
                    ],
                },
                timeout=35,
            )
            row["apply_status"] = submit.status_code
            if submit.status_code != 202:
                try:
                    row["apply_error"] = submit.json().get("code")
                except ValueError:
                    row["apply_error"] = "non-JSON"
                continue
            status = poll(
                client,
                f"/api/v1/remediations/{submit.json()['remediationId']}",
                session,
                "REMEDIATING",
                210,
            )
            row["remediation_status"] = status.get("status", status)
            if status.get("status") != "REMEDIATED":
                row["remediation_error"] = (status.get("error") or {}).get("code")
                continue
            output = client.request(
                "GET",
                f"/api/v1/outputs/{status['outputId']}/workbook",
                identity="userA",
                session_header=False,
                headers=headers,
                timeout=30,
                max_bytes=33_554_432,
            )
            try:
                row["comparator_problems"] = compare_packages(
                    variant.data, output.content, [plan(variant)]
                )
            except PackageError as exc:
                row["comparator_problems"] = [f"unreadable output: {exc}"]
            out_base = f"/api/v1/workbooks/{ids['workbookId']}/versions/{status['outputVersionId']}"
            out_audit = poll(
                client, f"{out_base}/audits/{status['auditId']}", session, "PROCESSING", 80
            )
            row["output_worksheet_name_findings"] = [
                f["target"]
                for f in out_audit.get("findings", [])
                if f["ruleId"] == "WORKSHEET_NAME"
            ]
    finally:
        client.request(
            "POST",
            "/test/setup",
            identity="test-admin",
            json={
                "session_id": f"t9-rename-cleanup-{uuid.uuid4().hex[:12]}",
                "setup": {"cleanup_session_ids": [session]},
            },
            timeout=30,
        )
    summary = {
        "variants": len(results),
        "classification_disagreements": [
            r["variant"] for r in results if not r.get("classification_matches")
        ],
        "rewrite_disagreements": [r["variant"] for r in results if r.get("comparator_problems")],
        "apply_refused": [
            r["variant"] for r in results if r.get("apply_status") not in (None, 202)
        ],
    }
    return {"namespace": session, "summary": summary, "results": results}


def local_team9(stack: ExitStack) -> tuple[str, dict[str, str]]:
    """Start the real Team 9 API and worker on private temporary state; return URL and tokens."""
    state = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="team9-rename-")))
    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        port = int(probe_socket.getsockname()[1])
    tokens = {name: secrets.token_urlsafe(24) for name in ("A", "B", "reviewer", "admin")}
    principals = {
        hashlib.sha256(tokens[name].encode()).hexdigest(): {"subject": name, "role": role}
        for name, role in (
            ("A", "user"),
            ("B", "user"),
            ("reviewer", "reviewer"),
            ("admin", "test_admin"),
        )
    }
    env = {
        "PATH": os.defpath,
        "PYTHONPATH": str(REPO),
        "APP_ENV": "test",
        "RULESET_VERSION": "2.0.0",
        "METADATA_DB_PATH": str(state / "system.sqlite3"),
        "BLOB_ROOT": str(state / "sources"),
        "SYSTEM_TOKEN_HASHES_JSON": json.dumps(principals),
        "REVIEWER_GRANTS_JSON": json.dumps({"reviewer": ["A"]}),
        "TEST_SESSION_USERS_JSON": json.dumps({"admin": ["A", "B", "reviewer"]}),
        "ENABLE_TEST_INTERFACE": "true",
        "DEPLOYMENT_SHA": "unknown",
    }
    for command in (
        [
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
        ],
        [sys.executable, "-m", "app.jobs.worker"],
    ):
        with (state / "process.log").open("ab") as log:
            process = subprocess.Popen(
                command, cwd=state, env=env, stdout=subprocess.DEVNULL, stderr=log
            )
        stack.callback(process.wait, 10)
        stack.callback(process.terminate)
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while True:
        try:
            if httpx.get(f"{url}/api/v1/health", timeout=1, trust_env=False).status_code == 200:
                break
        except httpx.TransportError:
            pass
        if time.monotonic() >= deadline:
            raise SystemExit(
                "local Team 9 system did not start: "
                + (state / "process.log").read_text(errors="replace")[-2000:]
            )
        time.sleep(0.2)
    access = {
        "userA": tokens["A"],
        "userB": tokens["B"],
        "reviewer": tokens["reviewer"],
        "test-admin": tokens["admin"],
    }
    return url, access


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--target-url", help="an approved system base URL from tester config")
    target.add_argument("--local-team9", action="store_true", help="probe a private local system")
    parser.add_argument("--output", type=Path, help="write the JSON diagnostic here")
    args = parser.parse_args()
    with ExitStack() as stack:
        if args.local_team9:
            url, tokens = local_team9(stack)
            settings = TesterSettings(
                _env_file=None, allowed_target_urls=[url], TARGET_AUTH_MAP_JSON={url: tokens}
            )
        else:
            url, settings = args.target_url, TesterSettings()
        access = settings.target_auth_map.get(url)
        if url not in settings.allowed_target_urls or access is None:
            raise SystemExit("target is not an approved, configured base URL")
        with contract.use_pack("phase5"):
            client = TargetClient(url, access, settings.app_env, time.monotonic() + 1800)
            diagnostic = probe(client)
    diagnostic["target_url"] = url
    text = json.dumps(diagnostic, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(json.dumps(diagnostic["summary"] if "summary" in diagnostic else diagnostic, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
