"""Real engine through the Phase 4 HTTP API, SQLite, filesystem storage and worker children.

Unlike the backend workflow tests, nothing here is a double: the production engine
audits, preflights, edits, verifies preservation and re-audits through the backend.
"""

import hashlib
import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit.engine import OoxmlWorkbookEngine
from app.config import SystemSettings
from app.contracts.models import Principal
from app.jobs.worker import AuditWorker
from app.main import create_app
from app.services.assembly import assemble
from tests.unit.phase4_builder import Link, Sheet, Workbook, picture_drawing

AUTH = {"Authorization": "Bearer owner"}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = SystemSettings(
        _env_file=None,
        ruleset_version="2.0.0",
        metadata_db_path=tmp_path / "metadata.sqlite3",
        blob_root=tmp_path / "sources",
        SYSTEM_TOKEN_HASHES_JSON={
            hashlib.sha256(b"owner").hexdigest(): Principal(subject="owner", role="user")
        },
        ui_router_factory=None,
    )
    with TestClient(create_app(services=assemble(settings, engine=OoxmlWorkbookEngine()))) as test:
        yield test


def tick(client: TestClient) -> None:
    assert AuditWorker(client.app.state.services).run_once()


def workbook() -> bytes:
    return Workbook(
        [
            Sheet(
                "Sheet1",
                {"A1": "Name", "B1": "Score", "A2": "Ada", "B2": 9, "D5": "click here"},
                links=[Link("D5", url="https://example.edu/scores")],
                drawing=picture_drawing("3"),
                merges=["F1:G1"],
            )
        ]
    ).build()


def test_health_reports_phase4_ruleset_with_real_engine(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "AVAILABLE",
        "apiVersion": "1.0",
        "rulesetVersion": "2.0.0",
    }


def test_upload_review_apply_download_and_reaudit(client: TestClient) -> None:
    data = workbook()
    accepted = client.post(
        "/api/v1/workbooks", files={"file": ("scores.xlsx", data)}, headers=AUTH
    ).json()
    path = f"/api/v1/workbooks/{accepted['workbookId']}/versions/{accepted['versionId']}"
    audit_path = f"{path}/audits/{accepted['auditId']}"
    tick(client)
    audit = client.get(audit_path, headers=AUTH).json()
    assert audit["status"] == "REMEDIATION_AVAILABLE"
    rules = [(f["ruleId"], f["classification"]) for f in audit["findings"]]
    assert rules == [
        ("ALT_TEXT", "REQUIRES_USER_INPUT"),
        ("LINK_TEXT", "REQUIRES_USER_INPUT"),
        ("TABLE_HEADER", "REQUIRES_USER_INPUT"),
        ("WORKSHEET_NAME", "REQUIRES_USER_INPUT"),
    ]
    review = client.get(f"{audit_path}/review", headers=AUTH).json()
    current = {item["findingId"]: item["currentValue"] for item in review["items"]}
    values = {
        "ALT_TEXT": {"title": "Score chart", "description": "Scores by student"},
        "LINK_TEXT": {"text": "Score details"},
        "TABLE_HEADER": current[audit["findings"][2]["findingId"]],
        "WORKSHEET_NAME": None,
    }
    decisions = [
        {"findingId": f["findingId"], "action": "APPLY", "value": values[f["ruleId"]]}
        if values[f["ruleId"]] is not None
        else {"findingId": f["findingId"], "action": "DECLINE"}
        for f in audit["findings"]
    ]
    response = client.post(
        f"{path}/remediations",
        json={"auditId": accepted["auditId"], "decisions": decisions},
        headers=AUTH | {"Idempotency-Key": "dylan-http-1"},
    )
    assert response.status_code == 202, response.text
    remediation_id = response.json()["remediationId"]
    tick(client)
    status = client.get(f"/api/v1/remediations/{remediation_id}", headers=AUTH).json()
    assert status["status"] == "REMEDIATED", status
    output = client.get(f"/api/v1/outputs/{status['outputId']}/workbook", headers=AUTH)
    assert output.status_code == 200
    with zipfile.ZipFile(io.BytesIO(output.content)) as archive:
        assert "xl/tables/table1.xml" in archive.namelist()
    # The output audit is committed with the output in the same worker transaction.
    output_audit = client.get(
        f"/api/v1/workbooks/{accepted['workbookId']}/versions/{status['outputVersionId']}"
        f"/audits/{status['auditId']}",
        headers=AUTH,
    ).json()
    assert [f["ruleId"] for f in output_audit["findings"]] == ["WORKSHEET_NAME"]
    report = client.get(
        f"/api/v1/workbooks/{accepted['workbookId']}/versions/{status['outputVersionId']}"
        f"/audits/{status['auditId']}/report",
        headers=AUTH,
    ).json()
    assert [change["ruleId"] for change in report["approvedChanges"]] == [
        "ALT_TEXT",
        "LINK_TEXT",
        "TABLE_HEADER",
    ]
