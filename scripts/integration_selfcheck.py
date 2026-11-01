"""Exercise the real API, durable worker and independent tester over local HTTP.

Uses private temporary state and credentials; never loads the operator's .env.
No cloud or other team's endpoints are contacted. Optional evidence contains no credentials.
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
from collections import Counter
from pathlib import Path

import httpx

from tester.suite.manifest import load_suite
from tester.suite.records import run_record
from tester.suite.registry import names as check_names

REPO = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def stop(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument(
        "--phase4",
        action="store_true",
        help="Run the real ruleset 2.0.0 system and corrected Phase 4 independent suite",
    )
    args = parser.parse_args()
    manifest_path = (
        REPO / "tests/fixtures" / ("manifest-phase4.json" if args.phase4 else "manifest.json")
    )
    suite = load_suite(manifest_path, check_names())
    output = args.evidence_dir.resolve() if args.evidence_dir else None
    if output:
        output.mkdir(parents=True, exist_ok=True)
    system_port, tester_port = free_port(), free_port()
    target = f"http://127.0.0.1:{system_port}"
    tester_url = f"http://127.0.0.1:{tester_port}"
    tokens = {
        name: secrets.token_urlsafe(32) for name in ("A", "B", "reviewer", "admin", "operator")
    }
    principals = {
        hashlib.sha256(tokens[name].encode()).hexdigest(): {"subject": name, "role": role}
        for name, role in (
            ("A", "user"),
            ("B", "user"),
            ("reviewer", "reviewer"),
            ("admin", "test_admin"),
        )
    }
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    code_dirty = bool(
        subprocess.check_output(
            [
                "git",
                "status",
                "--porcelain",
                "--",
                "app",
                "tester",
                "scripts",
                "tests",
                "pyproject.toml",
                "uv.lock",
                "deploy",
            ],
            cwd=REPO,
            text=True,
        ).strip()
    )
    with tempfile.TemporaryDirectory(prefix="team9-integrated-") as directory:
        state = Path(directory)
        environment = {
            "PATH": os.defpath,
            "PYTHONPATH": str(REPO),
            "APP_ENV": "test",
            "RULESET_VERSION": suite.manifest.ruleset_version,
            "ENABLE_TEST_AUDIT_FAULTS": "true" if args.phase4 else "false",
            "TESTER_RUN_TIMEOUT_SECONDS": "1800" if args.phase4 else "600",
            "DEPLOYMENT_SHA": revision,
            "METADATA_DB_PATH": str(state / "system.sqlite3"),
            "BLOB_ROOT": str(state / "sources"),
            "TESTER_DB_PATH": str(state / "tester.sqlite3"),
            "SYSTEM_TOKEN_HASHES_JSON": json.dumps(principals),
            "REVIEWER_GRANTS_JSON": json.dumps({"reviewer": ["A"]}),
            "TEST_SESSION_USERS_JSON": json.dumps({"admin": ["A", "B", "reviewer"]}),
            "ENABLE_TEST_INTERFACE": "true",
            "ENABLE_CODE_MEASUREMENT": "true",
            "TESTER_OPERATOR_TOKEN_HASHES_JSON": json.dumps(
                {hashlib.sha256(tokens["operator"].encode()).hexdigest(): "operator"}
            ),
            "ALLOWED_TARGET_URLS": json.dumps([target]),
            "TARGET_AUTH_MAP_JSON": json.dumps(
                {
                    target: {
                        "userA": tokens["A"],
                        "userB": tokens["B"],
                        "reviewer": tokens["reviewer"],
                        "test-admin": tokens["admin"],
                    }
                }
            ),
            "FIXTURE_MANIFEST_PATH": str(manifest_path),
        }
        processes: list[subprocess.Popen[bytes]] = []

        def start(module: str, port: int | None = None) -> subprocess.Popen[bytes]:
            command = (
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    module,
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--no-access-log",
                ]
                if port
                else [sys.executable, "-m", module]
            )
            if module == "app.jobs.worker" and args.phase4:
                # Exercise the same bounded worker capacity as production Compose.
                command.extend(["--concurrency", "2"])
            # The actual worker emits allowlisted JSON events without workbook
            # contents or credentials. Retain those events for CI diagnosis.
            with (state / "worker.log").open("ab") as worker_log:
                process = subprocess.Popen(
                    command,
                    cwd=state,
                    env=environment,
                    stdout=subprocess.DEVNULL,
                    stderr=worker_log if module == "app.jobs.worker" else subprocess.DEVNULL,
                )
            processes.append(process)
            if port:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f"{module} exited during startup")
                    try:
                        if (
                            httpx.get(
                                f"http://127.0.0.1:{port}/docs", timeout=1, trust_env=False
                            ).status_code
                            == 200
                        ):
                            return process
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                raise TimeoutError(f"{module} did not start")
            return process

        try:
            system = start("app.main:create_app", system_port)
            start("tester.main:create_app", tester_port)
            with httpx.Client(
                base_url=target,
                trust_env=False,
                timeout=40,
                headers={"Authorization": f"Bearer {tokens['A']}"},
            ) as client:
                assert client.get("/api/v1/health").status_code == 200
                assert client.get("/").status_code == 200
                source = (REPO / "tests/fixtures/synthetic/dylan/seeded_issues.xlsx").read_bytes()
                uploaded = client.post("/api/v1/workbooks", files={"file": ("seeded.xlsx", source)})
                assert uploaded.status_code == 202, uploaded.text
                ids = uploaded.json()
                path = f"/api/v1/workbooks/{ids['workbookId']}/versions/{ids['versionId']}"
                audit_path = f"{path}/audits/{ids['auditId']}"
                assert client.get(audit_path).json()["status"] == "PROCESSING"
                stop(system)
                processes.remove(system)
                start("app.main:create_app", system_port)
                assert client.get(audit_path).json()["status"] == "PROCESSING"
                worker = start("app.jobs.worker")
                deadline = time.monotonic() + 75
                while True:
                    audit = client.get(audit_path).json()
                    if audit["status"] != "PROCESSING" or time.monotonic() >= deadline:
                        break
                    time.sleep(0.2)
                assert audit["status"] == (
                    "REMEDIATION_AVAILABLE" if args.phase4 else "REVIEW_REQUIRED"
                ), audit
                assert audit["findings"] and audit["error"] is None
                assert client.get(path).json()["sha256"] == hashlib.sha256(source).hexdigest()
                assert worker.poll() is None
                print(
                    "Real upload, API restart, separate worker, findings and source hash: pass",
                    flush=True,
                )
            with httpx.Client(
                base_url=tester_url,
                trust_env=False,
                timeout=1830 if args.phase4 else 630,
                headers={"Authorization": f"Bearer {tokens['operator']}"},
            ) as client:
                setup = client.post("/test/setup", json={"target_url": target})
                assert setup.status_code == 201, setup.text
                sid = setup.json()["session_id"]
                run = client.post("/test/run", json={"session_id": sid})
                stored = client.get("/test/report", params={"session_id": sid})
                assert run.status_code == 200, run.text
                assert stored.content == run.content
                assert client.post("/test/run", json={"session_id": sid}).status_code == 409
                report = stored.json()
                counts = dict(Counter(result["outcome"] for result in report["results"]))
                if output:
                    (output / "report.json").write_bytes(stored.content)
                    suite = load_suite(manifest_path, check_names())
                    record = run_record(
                        stored.content,
                        suite.manifest,
                        tester_url=tester_url,
                        target_revision=revision,
                    )
                    record["source_working_tree_dirty"] = code_dirty
                    record["checkpoint"] = {
                        "real_engine": True,
                        "api_restart_retained_upload": True,
                        "separate_worker": True,
                        "worker_concurrency": 2 if args.phase4 else 1,
                        "source_sha256_unchanged": True,
                        "report_byte_identical": True,
                        "repeat_run_status": 409,
                    }
                    (output / "run-record.json").write_text(json.dumps(record, indent=2) + "\n")
                print(json.dumps({"counts": counts, "coverage": report["coverage"]}), flush=True)
                for result in report["results"]:
                    if result["outcome"] in ("fail", "error"):
                        print(json.dumps(result), flush=True)
                if counts.get("fail", 0) or counts.get("error", 0):
                    return 1
            acceptance = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "tests/acceptance"],
                cwd=REPO,
                env=environment
                | {
                    "TEAM9_SUITE_MANIFEST": str(manifest_path),
                    "TEAM9_RUN_READ_TIMEOUT": "1830" if args.phase4 else "630",
                    "TEAM9_TESTER_URL": tester_url,
                    "TEAM9_TARGET_URL": target,
                    "TEAM9_TESTER_TOKEN": tokens["operator"],
                },
                check=False,
            )
            return acceptance.returncode
        finally:
            for process in reversed(processes):
                stop(process)
            if output:
                events = []
                for line in (state / "worker.log").read_text().splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and event.get("event") == "job_finished":
                        events.append(json.dumps(event))
                (output / "worker-events.jsonl").write_text("\n".join(events) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
