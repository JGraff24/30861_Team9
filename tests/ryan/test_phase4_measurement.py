"""Real Phase 4 worker measurements and read-only instructor report evidence."""

import hashlib
import json
import sqlite3

from fastapi.testclient import TestClient

from app.config import SystemSettings
from app.contracts.models import Principal
from app.jobs.worker import AuditWorker
from app.main import create_app
from tests.unit.test_dylan_phase4_http import workbook


def test_phase4_measurement_and_report_isolation(tmp_path):
    principals = {"owner": "user", "reviewer": "reviewer", "admin": "test_admin"}
    settings = SystemSettings(
        _env_file=None,
        metadata_db_path=tmp_path / "metadata.sqlite3",
        blob_root=tmp_path / "blobs",
        enable_test_interface=True,
        enable_code_measurement=True,
        enable_test_audit_faults=True,
        SYSTEM_TOKEN_HASHES_JSON={
            hashlib.sha256(token.encode()).hexdigest(): Principal(subject=token, role=role)
            for token, role in principals.items()
        },
        TEST_SESSION_USERS_JSON={"admin": ["owner", "reviewer"]},
        REVIEWER_GRANTS_JSON={"reviewer": ["owner"]},
    )
    admin = {"Authorization": "Bearer admin"}
    user = {"Authorization": "Bearer owner", "X-Test-Session": "measured"}
    with TestClient(create_app(settings)) as client:
        for sid in ("measured", "untouched"):
            assert (
                client.post(
                    "/test/setup", json={"session_id": sid, "setup": {}}, headers=admin
                ).status_code
                == 201
            )
        source = workbook()
        response = client.post(
            "/api/v1/workbooks", files={"file": ("review.xlsx", source)}, headers=user
        )
        assert response.status_code == 202, response.text
        ids = response.json()
        path = f"/api/v1/workbooks/{ids['workbookId']}/versions/{ids['versionId']}"
        audit_path = path + f"/audits/{ids['auditId']}"
        worker = AuditWorker(client.app.state.services)
        assert worker.run_once()
        audit = client.get(audit_path, headers=user).json()
        assert audit["status"] == "REMEDIATION_AVAILABLE", audit
        reviewer = user | {"Authorization": "Bearer reviewer"}
        assert client.get("/ui/identity", headers=reviewer).json() == {"role": "reviewer"}
        assert all(
            not item["allowedActions"]
            for item in client.get(audit_path + "/review", headers=reviewer).json()["items"]
        )
        baseline = client.get("/test/report?session_id=measured", headers=admin).json()
        report = client.get(audit_path + "/report", headers=user).content
        values = {
            "ALT_TEXT": {"title": "Scores", "description": "Scores for each student"},
            "LINK_TEXT": {"text": "Student score details"},
            "TABLE_HEADER": {"headerRow": 1},
            "WORKSHEET_NAME": {"name": "Student scores"},
        }
        decision_body = {
            "auditId": ids["auditId"],
            "decisions": [
                {"findingId": f["findingId"], "action": "APPLY", "value": values[f["ruleId"]]}
                for f in audit["findings"]
            ],
        }
        response = client.post(
            path + "/remediations",
            json=decision_body,
            headers=user | {"Idempotency-Key": "ryan-measured-corrections"},
        )
        assert response.status_code == 202, response.text
        assert worker.run_once()
        result = client.get(
            "/api/v1/remediations/" + response.json()["remediationId"], headers=user
        ).json()
        assert result["status"] == "REMEDIATED", result
        assert client.get(path + "/source", headers=user).content == source
        assert client.get(audit_path + "/report", headers=user).content == report
        with sqlite3.connect(settings.metadata_db_path) as db:
            rows_before = db.execute("SELECT * FROM measurements ORDER BY invocation_id").fetchall()
        measurements = [json.loads(row[2]) for row in rows_before]
        assert (
            len(measurements) >= 8
        )  # intake, audit, preflight, edit, validation, both verifiers, audit
        assert all(
            m["session_id"] == "measured" and m["revision"] == "unknown" for m in measurements
        )
        assert all(m["unavailable_reason"] is None for m in measurements)
        scope = measurements[0]["executable_lines"]
        assert all(m["executable_lines"] == scope for m in measurements)
        for filename in ("app/workbooks/editor.py", "app/workbooks/preservation.py"):
            assert any(m["covered_lines"].get(filename) for m in measurements)
        covered = {
            (p, line)
            for m in measurements
            for p, lines in m["covered_lines"].items()
            for line in lines
        }
        total = sum(len(lines) for lines in scope.values())
        final = client.get("/test/report?session_id=measured", headers=admin)
        assert final.status_code == 200
        measure = final.json()["coverage"]["code"]
        assert measure["covered"] == len(covered) > baseline["coverage"]["code"]["covered"]
        assert measure["total"] == total
        assert "excludes API, storage, UI" in measure["basis"]
        assert (
            client.get("/test/report?session_id=measured", headers=admin).content == final.content
        )
        with sqlite3.connect(settings.metadata_db_path) as db:
            assert (
                db.execute("SELECT * FROM measurements ORDER BY invocation_id").fetchall()
                == rows_before
            )
        other = client.get("/test/report?session_id=untouched", headers=admin).json()
        assert other["coverage"]["code"]["covered"] is None
        assert client.get("/ui/identity", headers=admin).status_code == 403
        assert client.get("/ui/identity").status_code == 401
