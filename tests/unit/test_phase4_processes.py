"""Live Phase 4 backend smoke with real HTTP, storage, DB and worker processes.

CorrectionEngineDouble deliberately supplies synthetic six-rule audit behavior;
this test proves backend integration and restart persistence, not Dylan's rule
or editor acceptance. Intake validation parses real OOXML through the double.
All credentials, records and listeners belong to this temporary local test.
"""

import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx


def test_live_phase4_upload_remediation_restart_and_downloads(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "tests/fixtures/synthetic/clean.xlsx").read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    tokens = {actor: secrets.token_urlsafe(32) for actor in ("owner", "reviewer", "other", "admin")}
    roles = {"owner": "user", "reviewer": "reviewer", "other": "user", "admin": "test_admin"}
    environment = {
        "PATH": os.defpath,
        "PYTHONPATH": str(root),
        "METADATA_DB_PATH": str(tmp_path / "system.sqlite3"),
        "BLOB_ROOT": str(tmp_path / "sources"),
        "RULESET_VERSION": "2.0.0",
        "ENGINE_FACTORY": "tests.unit.test_phase4_worker:CorrectionEngineDouble",
        "SYSTEM_TOKEN_HASHES_JSON": json.dumps(
            {
                hashlib.sha256(token.encode()).hexdigest(): {"subject": actor, "role": roles[actor]}
                for actor, token in tokens.items()
            }
        ),
        "REVIEWER_GRANTS_JSON": json.dumps({"reviewer": ["owner"]}),
        "PRIVATE_TEST_SECRET": secrets.token_urlsafe(32),
    }
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    log_path = tmp_path / "api.stderr"
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
        with log_path.open("ab") as logs:
            process = subprocess.Popen(
                command,
                cwd=tmp_path,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=logs,
            )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                assert process.poll() is None, log_path.read_text()
                try:
                    response = httpx.get(
                        f"{base}/api/v1/health",
                        timeout=0.3,
                        trust_env=False,
                        follow_redirects=False,
                    )
                    if response.status_code == 200:
                        assert response.json() == {
                            "status": "AVAILABLE",
                            "apiVersion": "1.0",
                            "rulesetVersion": "2.0.0",
                        }
                        return process
                except httpx.TransportError:
                    pass
                time.sleep(0.05)
            raise TimeoutError("Temporary Phase 4 API startup exceeded 10 seconds")
        except BaseException:
            process.terminate()
            process.wait(timeout=5)
            raise

    def worker() -> tuple[list[dict[str, object]], str]:
        result = subprocess.run(
            [sys.executable, "-m", "app.jobs.worker", "--once"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        events = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
        for secret in (*tokens.values(), environment["PRIVATE_TEST_SECRET"]):
            assert secret not in result.stderr and secret not in result.stdout
        assert "Synthetic worker test finding" not in result.stderr
        return events, result.stderr

    def auth(actor: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {tokens[actor]}"}

    process = start()
    try:
        with httpx.Client(
            base_url=base,
            headers=auth("owner"),
            timeout=10,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            uploaded = client.post(
                "/api/v1/workbooks", files={"file": ("Research 研究.xlsx", source)}
            )
            assert uploaded.status_code == 202, uploaded.text
            assert uploaded.headers["x-request-id"]
            ids = uploaded.json()
            assert set(ids) == {"workbookId", "versionId", "auditId", "status"}
            assert ids["status"] == "PROCESSING"
            version_path = f"/api/v1/workbooks/{ids['workbookId']}/versions/{ids['versionId']}"
            audit_path = f"{version_path}/audits/{ids['auditId']}"
            history_path = f"/api/v1/workbooks/{ids['workbookId']}/history"
            assert client.get(audit_path).json()["status"] == "PROCESSING"
            audit_events, _ = worker()
            audit_terminal = [
                event for event in audit_events if event.get("event") == "job_finished"
            ]
            assert len(audit_terminal) == 1
            assert audit_terminal[0]["code"] == "AUDIT_SUCCEEDED"
            assert audit_terminal[0]["level"] == "INFO"
            assert audit_terminal[0]["auditId"] == ids["auditId"]
            audited = client.get(audit_path)
            assert audited.status_code == 200
            audit = audited.json()
            assert audit["status"] == "REVIEW_REQUIRED"
            assert audit["rulesetVersion"] == "2.0.0"
            assert audit["limitations"] == ["Synthetic audit double; does not evaluate six rules."]
            source_report = client.get(audit_path + "/report")
            assert source_report.status_code == 200
            review = client.get(audit_path + "/review")
            assert review.status_code == 200
            assert review.json()["items"][0]["allowedActions"] == ["DECLINE", "MANUAL_REVIEW"]
            request = {
                "auditId": ids["auditId"],
                "decisions": [
                    {"findingId": audit["findings"][0]["findingId"], "action": "DECLINE"}
                ],
            }
            remediation_path = version_path + "/remediations"
            keyed = {"Idempotency-Key": "temporary-process-review"}
            accepted = client.post(remediation_path, json=request, headers=keyed)
            assert accepted.status_code == 202, accepted.text
            assert set(accepted.json()) == {"remediationId", "sourceVersionId", "status"}
            assert accepted.json()["status"] == "REMEDIATING"
            remediation_id = accepted.json()["remediationId"]
            poll_path = f"/api/v1/remediations/{remediation_id}"
            pending = client.get(poll_path)
            assert pending.status_code == 200
            assert pending.json() == {
                "remediationId": remediation_id,
                "status": "REMEDIATING",
                "sourceVersionId": ids["versionId"],
                "outputVersionId": None,
                "outputId": None,
                "auditId": None,
                "error": None,
            }
            remediation_events, _ = worker()
            terminal = [
                event for event in remediation_events if event.get("event") == "job_finished"
            ]
            assert len(terminal) == 1
            assert terminal[0]["code"] == "REMEDIATION_SUCCEEDED"
            assert terminal[0]["level"] == "INFO"
            assert terminal[0]["remediationId"] == remediation_id
            assert terminal[0]["versionId"] == ids["versionId"]
            assert terminal[0]["stage"] == "publication"
            assert isinstance(terminal[0]["duration"], (int, float))
            completed_response = client.get(poll_path)
            assert completed_response.status_code == 200
            completed = completed_response.json()
            assert completed["status"] == "REMEDIATED" and completed["error"] is None
            assert completed["outputVersionId"] != ids["versionId"]
            generated_path = (
                f"/api/v1/workbooks/{ids['workbookId']}/versions/{completed['outputVersionId']}"
            )
            output_path = f"/api/v1/outputs/{completed['outputId']}/workbook"
            report_path = generated_path + f"/audits/{completed['auditId']}/report"
            output_before_restart = client.get(output_path)
            report_before_restart = client.get(report_path)
            history_before_restart = client.get(history_path)
            assert output_before_restart.status_code == 200
            assert report_before_restart.status_code == 200
            assert report_before_restart.headers["content-type"] == "application/json"
            assert report_before_restart.headers["cache-control"] == "no-store"
            assert (
                f"audit-{completed['auditId']}.json"
                in report_before_restart.headers["content-disposition"]
            )
            assert history_before_restart.status_code == 200
            process.terminate()
            process.wait(timeout=5)
            process = start()
            assert client.get(poll_path).json() == completed
            assert client.get(output_path).content == output_before_restart.content == source
            assert client.get(report_path).content == report_before_restart.content
            assert client.get(history_path).content == history_before_restart.content
            assert client.get(audit_path + "/report").content == source_report.content
            assert client.get(audit_path).json() == audit
            generated = client.get(generated_path)
            assert generated.status_code == 200
            assert generated.json()["sha256"] == digest
            assert generated.json()["sourceVersionId"] == ids["versionId"]
            assert generated.json()["status"] == "REMEDIATED"
            assert client.get(version_path).json()["sha256"] == digest
            for path in (version_path + "/source", generated_path + "/source", output_path):
                download = client.get(path)
                assert download.status_code == 200
                assert download.content == source
                assert hashlib.sha256(download.content).hexdigest() == digest
                assert (
                    download.headers["content-type"]
                    == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
                assert download.headers["content-length"] == str(len(source))
                assert download.headers["cache-control"] == "no-store"
                assert download.headers["content-disposition"].startswith("attachment;")
                assert "filename*=UTF-8''" in download.headers["content-disposition"]
                assert download.headers["x-request-id"]
            report = report_before_restart.json()
            assert report["sha256"] == digest
            assert report["sourceVersionId"] == ids["versionId"]
            assert report["remediationId"] == remediation_id
            assert report["outputVersionId"] == completed["outputVersionId"]
            assert report["outputId"] == completed["outputId"]
            assert report["decisions"][0]["sourceAuditId"] == ids["auditId"]
            assert report["decisions"][0]["action"] == "DECLINE"
            assert report["decisions"][0]["value"] is None
            assert report["approvedChanges"] == []
            assert report["unresolvedFindings"] == report["findings"]
            history = history_before_restart.json()
            assert [entry["versionId"] for entry in history["versions"]] == [
                ids["versionId"],
                completed["outputVersionId"],
            ]
            assert [entry["auditId"] for entry in history["audits"]] == [
                ids["auditId"],
                completed["auditId"],
            ]
            replayed = client.post(remediation_path, json=request, headers=keyed)
            assert replayed.status_code == 202
            assert replayed.content == accepted.content
            assert client.get(history_path).content == history_before_restart.content
            for path in (version_path + "/source", output_path, report_path, history_path):
                assert client.get(path, headers=auth("reviewer")).status_code == 200
                denied = client.get(path, headers=auth("other"))
                assert denied.status_code == 403
                assert denied.json()["code"] == "FORBIDDEN"
                assert client.get(path, headers=auth("admin")).status_code == 403
                assert client.get(path, headers={"Authorization": ""}).status_code == 401
            assert (
                client.post(version_path + "/audits", json={}, headers=auth("reviewer")).status_code
                == 403
            )
        logs = log_path.read_text()
        for secret in (*tokens.values(), environment["PRIVATE_TEST_SECRET"]):
            assert secret not in logs
        assert "Synthetic worker test finding" not in logs
    finally:
        process.terminate()
        process.wait(timeout=5)
