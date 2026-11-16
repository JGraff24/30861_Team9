"""Phase 5 §7/§11: scheduler-dispatched audits reuse the six-rule engine unchanged.

Drives Jackson's LifecycleService (schedule configuration and isolated clock) with the
real API, SQLite, filesystem storage, production engine and bounded worker children.
Ryan owns the HTTP clock route, so the clock is advanced through the service exactly as
that route will call it. The module is skipped until the Phase 5 backend is present.

Engine obligations checked here: a scheduled audit of a stored version produces exactly
the findings, limitations, status and review context of a manual audit of the same
bytes; it adds a fresh audit without a new version; earlier audits, report downloads and
source bytes stay byte-identical; a generated newest version is audited like its
publication audit; and an injected scheduled-audit failure preserves everything and
leaves the next interval able to succeed with the same findings.
"""

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

lifecycle_module = pytest.importorskip(
    "app.services.lifecycle", reason="Phase 5 scheduling backend (Jackson) is not installed"
)

from app.config import SystemSettings  # noqa: E402
from app.contracts.models import Principal  # noqa: E402
from app.jobs.worker import AuditWorker  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.assembly import assemble  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC = ROOT / "tests" / "fixtures" / "phase4" / "synthetic"
ADMIN = Principal(subject="admin", role="test_admin")


def headers(session: str, token: str = "owner") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Test-Session": session}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = SystemSettings(
        _env_file=None,
        ruleset_version="2.0.0",
        metadata_db_path=tmp_path / "metadata.sqlite3",
        blob_root=tmp_path / "blobs",
        enable_test_interface=True,
        enable_test_audit_faults=True,
        enable_test_clock=True,
        enable_code_measurement=False,
        ui_router_factory=None,
        TEST_SESSION_USERS_JSON={"admin": ["owner"]},
        SYSTEM_TOKEN_HASHES_JSON={
            hashlib.sha256(name.encode()).hexdigest(): Principal(subject=name, role=role)
            for name, role in (("admin", "test_admin"), ("owner", "user"))
        },
    )
    with TestClient(create_app(services=assemble(settings))) as active:
        yield active


def prepare(client: TestClient, session: str, setup: dict | None = None) -> None:
    response = client.post(
        "/test/setup",
        json={"session_id": session, "setup": setup or {}},
        headers={"Authorization": "Bearer admin"},
    )
    assert response.status_code == 201, response.text


def drain(client: TestClient) -> None:
    worker = AuditWorker(client.app.state.services)
    assert worker.run_once()
    while worker.run_once():
        pass


def advance(client: TestClient, session: str, when: str) -> list[str]:
    service = lifecycle_module.LifecycleService(client.app.state.services)
    return list(service.advance_clock(ADMIN, session, {"currentTime": when}).triggeredAudits)


def get(client: TestClient, session: str, path: str) -> dict:
    response = client.get(path, headers=headers(session))
    assert response.status_code == 200, response.text
    return response.json()


def comparable(audit: dict) -> dict:
    return {key: audit[key] for key in ("rulesetVersion", "status", "findings", "limitations")}


def snapshot(client: TestClient, session: str, wid: str, vid: str, aid: str) -> dict:
    path = f"/api/v1/workbooks/{wid}/versions/{vid}"
    audit = client.get(f"{path}/audits/{aid}", headers=headers(session))
    report = client.get(f"{path}/audits/{aid}/report", headers=headers(session))
    source = client.get(f"{path}/source", headers=headers(session))
    assert audit.status_code == report.status_code == source.status_code == 200
    return {"audit": audit.content, "report": report.content, "source": source.content}


def review(client: TestClient, session: str, wid: str, vid: str, aid: str) -> list:
    path = f"/api/v1/workbooks/{wid}/versions/{vid}/audits/{aid}/review"
    return get(client, session, path)["items"]


def upload(client: TestClient, session: str, name: str) -> tuple[dict, bytes]:
    data = (SYNTHETIC / f"{name}.xlsx").read_bytes()
    response = client.post(
        "/api/v1/workbooks", files={"file": (f"{name}.xlsx", data)}, headers=headers(session)
    )
    assert response.status_code == 202, response.text
    drain(client)
    return response.json(), data


def schedule(client: TestClient, session: str, wid: str) -> dict:
    response = client.put(
        f"/api/v1/workbooks/{wid}/schedule",
        json={"enabled": True, "intervalDays": 1},
        headers=headers(session),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_scheduled_audit_reproduces_manual_findings_and_keeps_history(client: TestClient) -> None:
    prepare(client, "s")
    accepted, data = upload(client, "s", "mixed_decisions")
    wid, vid, manual_id = accepted["workbookId"], accepted["versionId"], accepted["auditId"]
    path = f"/api/v1/workbooks/{wid}/versions/{vid}"
    manual = get(client, "s", f"{path}/audits/{manual_id}")
    assert manual["findings"] and manual["status"] == "REMEDIATION_AVAILABLE"
    manual_review = review(client, "s", wid, vid, manual_id)
    before = snapshot(client, "s", wid, vid, manual_id)
    assert before["source"] == data

    assert schedule(client, "s", wid)["versionId"] == vid
    triggered = advance(client, "s", "2027-01-02T00:00:00Z")
    assert len(triggered) == 1
    drain(client)
    scheduled = get(client, "s", f"{path}/audits/{triggered[0]}")
    assert comparable(scheduled) == comparable(manual)
    assert review(client, "s", wid, vid, scheduled["auditId"]) == manual_review
    report = json.loads(snapshot(client, "s", wid, vid, scheduled["auditId"])["report"])
    assert report["versionId"] == vid and report["auditId"] == scheduled["auditId"]
    assert report["findings"] == json.loads(before["report"])["findings"]

    # Several missed intervals coalesce into one more audit with identical findings.
    coalesced = advance(client, "s", "2027-01-05T12:00:00Z")
    assert len(coalesced) == 1
    drain(client)
    assert comparable(get(client, "s", f"{path}/audits/{coalesced[0]}")) == comparable(manual)

    assert snapshot(client, "s", wid, vid, manual_id) == before
    history = get(client, "s", f"/api/v1/workbooks/{wid}/history")
    assert [item["versionId"] for item in history["versions"]] == [vid]
    assert [item["auditId"] for item in history["audits"]] == [manual_id, *triggered, *coalesced]


def test_scheduled_audit_of_generated_newest_version_matches_publication(
    client: TestClient,
) -> None:
    prepare(client, "s")
    accepted, _ = upload(client, "s", "alt_supported")
    wid, vid = accepted["workbookId"], accepted["versionId"]
    audit = get(client, "s", f"/api/v1/workbooks/{wid}/versions/{vid}/audits/{accepted['auditId']}")
    picture = next(f for f in audit["findings"] if f["target"].endswith("#id=2"))
    response = client.post(
        f"/api/v1/workbooks/{wid}/versions/{vid}/remediations",
        json={
            "auditId": accepted["auditId"],
            "decisions": [
                {
                    "findingId": picture["findingId"],
                    "action": "APPLY",
                    "value": {"title": "Expense chart", "description": "Rent and travel costs."},
                }
            ],
        },
        headers=headers("s") | {"Idempotency-Key": "dylan-phase5-scheduled-generated"},
    )
    assert response.status_code == 202, response.text
    drain(client)
    status = get(client, "s", f"/api/v1/remediations/{response.json()['remediationId']}")
    assert status["status"] == "REMEDIATED", status
    output_vid, published_id = status["outputVersionId"], status["auditId"]
    published = get(
        client, "s", f"/api/v1/workbooks/{wid}/versions/{output_vid}/audits/{published_id}"
    )
    published_review = review(client, "s", wid, output_vid, published_id)
    before = snapshot(client, "s", wid, output_vid, published_id)
    original = snapshot(client, "s", wid, vid, accepted["auditId"])

    assert schedule(client, "s", wid)["versionId"] == output_vid
    triggered = advance(client, "s", "2027-01-02T00:00:00Z")
    assert len(triggered) == 1
    drain(client)
    scheduled = get(
        client, "s", f"/api/v1/workbooks/{wid}/versions/{output_vid}/audits/{triggered[0]}"
    )
    assert comparable(scheduled) == comparable(published)
    assert review(client, "s", wid, output_vid, triggered[0]) == published_review
    assert snapshot(client, "s", wid, output_vid, published_id) == before
    assert snapshot(client, "s", wid, vid, accepted["auditId"]) == original


def test_failed_scheduled_audit_preserves_history_and_next_interval_recovers(
    client: TestClient,
) -> None:
    prepare(client, "fault", {"faults": {"scheduled_audit_failures": 1}})
    accepted, data = upload(client, "fault", "mixed_decisions")
    wid, vid, manual_id = accepted["workbookId"], accepted["versionId"], accepted["auditId"]
    path = f"/api/v1/workbooks/{wid}/versions/{vid}"
    manual = get(client, "fault", f"{path}/audits/{manual_id}")
    before = snapshot(client, "fault", wid, vid, manual_id)

    schedule(client, "fault", wid)
    failed_id = advance(client, "fault", "2027-01-02T00:00:00Z")[0]
    drain(client)
    failed = get(client, "fault", f"{path}/audits/{failed_id}")
    assert failed["status"] == "PROCESSING_FAILED"
    assert failed["error"]["code"] == "TEST_SCHEDULED_AUDIT_FAILURE"
    assert failed["error"]["details"] == {"stage": "scheduled_audit", "testOnly": True}
    assert failed["findings"] == []
    failure_report = json.loads(
        client.get(f"{path}/audits/{failed_id}/report", headers=headers("fault")).content
    )
    assert failure_report["checksPerformed"] == [] and failure_report["findings"] == []
    assert snapshot(client, "fault", wid, vid, manual_id) == before
    assert before["source"] == data

    # The one-shot fault is consumed; the next interval audits the same bytes normally.
    recovered_id = advance(client, "fault", "2027-01-03T00:00:00Z")[0]
    drain(client)
    assert comparable(get(client, "fault", f"{path}/audits/{recovered_id}")) == comparable(manual)
    history = get(client, "fault", f"/api/v1/workbooks/{wid}/history")
    assert [item["status"] for item in history["audits"]] == [
        manual["status"],
        "PROCESSING_FAILED",
        manual["status"],
    ]
    assert snapshot(client, "fault", wid, vid, manual_id) == before
