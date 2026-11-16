"""Phase 5 backend integration: real SQLite, storage, six-rule engine and workers.

Calendar oracles are fixed specification examples, independent of production helpers.
Ryan's HTTP clock route is deliberately outside this backend contribution.
"""

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.config import SystemSettings
from app.contracts.errors import ProjectException, StorageError
from app.contracts.models import Principal
from app.jobs.worker import AuditWorker
from app.main import create_app
from app.services.assembly import assemble
from app.services.lifecycle import LifecycleService
from app.storage.locks import workbook_storage_lock
from app.storage.sqlite import SQLiteRepository
from tests.unit.phase4_builder import Sheet, Workbook

ADMIN = Principal(subject="admin", role="test_admin")
OWNER = Principal(subject="owner", role="user")
INITIAL = datetime(2027, 1, 1, tzinfo=UTC)


def headers(token: str = "owner", session: str | None = "s") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} | ({"X-Test-Session": session} if session else {})


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = SystemSettings(
        _env_file=None,
        metadata_db_path=tmp_path / "metadata.sqlite3",
        blob_root=tmp_path / "blobs",
        enable_test_interface=True,
        enable_test_audit_faults=True,
        enable_test_clock=True,
        enable_code_measurement=False,
        ui_router_factory=None,
        TEST_SESSION_USERS_JSON={"admin": ["owner", "foreign", "reviewer", "ungranted"]},
        REVIEWER_GRANTS_JSON={"reviewer": ["owner"]},
        SYSTEM_TOKEN_HASHES_JSON={
            hashlib.sha256(name.encode()).hexdigest(): Principal(subject=name, role=role)
            for name, role in (
                ("admin", "test_admin"),
                ("other-admin", "test_admin"),
                ("owner", "user"),
                ("foreign", "user"),
                ("reviewer", "reviewer"),
                ("ungranted", "reviewer"),
            )
        },
    )
    with TestClient(create_app(services=assemble(settings))) as active:
        prepare(active, "s")
        yield active


def prepare(client: TestClient, sid: str, setup: dict[str, object] | None = None) -> None:
    result = client.post(
        "/test/setup",
        json={"session_id": sid, "setup": setup or {}},
        headers=headers("admin", None),
    )
    assert result.status_code == 201, result.text


def upload(
    client: TestClient, *, session: str | None = "s", wid: str | None = None, clean: bool = True
) -> dict[str, str]:
    data = Workbook([Sheet("Expenses" if clean else "Sheet1")]).build()
    path = "/api/v1/workbooks" + (f"/{wid}/versions" if wid else "")
    result = client.post(
        path, files={"file": ("budget.xlsx", data)}, headers=headers(session=session)
    )
    assert result.status_code == 202, result.text
    return result.json()


def worker(client: TestClient) -> None:
    assert AuditWorker(client.app.state.services).run_once()


def lifecycle(client: TestClient) -> LifecycleService:
    return LifecycleService(client.app.state.services)


def repository(client: TestClient) -> SQLiteRepository:
    return lifecycle(client).repository


def advance(client: TestClient, value: str, sid: str = "s") -> object:
    return lifecycle(client).advance_clock(ADMIN, sid, {"currentTime": value})


def policy(client: TestClient, wid: str, body: object, sid: str | None = "s") -> dict[str, object]:
    result = client.put(
        f"/api/v1/workbooks/{wid}/schedule", json=body, headers=headers(session=sid)
    )
    assert result.status_code == 200, result.text
    return result.json()


def test_schedule_replacement_cadence_and_strict_wire(client: TestClient) -> None:
    accepted = upload(client)
    wid = accepted["workbookId"]
    path = f"/api/v1/workbooks/{wid}/schedule"
    assert client.get(path, headers=headers()).json() == {
        "workbookId": wid,
        "enabled": False,
        "intervalDays": None,
        "complianceDeadline": None,
        "versionId": accepted["versionId"],
        "nextAuditAt": None,
        "reminderDays": [90, 30, 7],
    }
    with repository(client)._db() as db:
        assert db.execute("SELECT count(*) FROM schedules").fetchone()[0] == 0
    first = policy(client, wid, {"enabled": True, "intervalDays": 1})
    assert first["nextAuditAt"] == "2027-01-02T00:00:00Z"
    for value in (0, 91, True, "30", 1.0, None):
        result = client.put(path, json={"enabled": True, "intervalDays": value}, headers=headers())
        assert result.status_code == 400, result.text
    for raw in (
        '{"enabled":true,"intervalDays":1,"intervalDays":2}',
        '{"enabled":true,"intervalDays":1e0}',
        '{"enabled":true,"intervalDays":NaN}',
        '{"enabled":true,"intervalDays":1,"extra":0}',
    ):
        assert (
            client.put(
                path, content=raw, headers=headers() | {"Content-Type": "application/json"}
            ).status_code
            == 400
        )
    for deadline in ("2027-02-29", "2027-4-01", "0000-01-01", "2027-01-01T00:00:00Z", 20270426):
        assert (
            client.put(
                path, json={"enabled": False, "complianceDeadline": deadline}, headers=headers()
            ).status_code
            == 400
        )
    assert policy(client, wid, {"enabled": True, "intervalDays": 1}) == first
    assert (
        policy(
            client, wid, {"enabled": True, "intervalDays": 1, "complianceDeadline": "2027-04-26"}
        )["nextAuditAt"]
        == first["nextAuditAt"]
    )
    assert policy(client, wid, {"enabled": False})["nextAuditAt"] is None
    assert (
        policy(client, wid, {"enabled": True, "intervalDays": 90})["nextAuditAt"]
        == "2027-04-01T00:00:00Z"
    )


def test_coalescing_busy_atomicity_newest_and_restart(client: TestClient) -> None:
    first = upload(client)
    wid = first["workbookId"]
    policy(client, wid, {"enabled": True, "intervalDays": 1})
    with pytest.raises(ProjectException, match="active work") as error:
        advance(client, "2027-01-02T00:00:00Z")
    assert error.value.code == "ACTIVE_OPERATION"
    assert repository(client).effective_now("s") == INITIAL
    worker(client)
    second = upload(client, wid=wid)
    worker(client)
    # Re-auditing the older version cannot change version creation order.
    path = f"/api/v1/workbooks/{wid}/versions/{first['versionId']}/audits"
    assert client.post(path, json={}, headers=headers()).status_code == 202
    worker(client)
    assert not advance(client, "2027-01-01T23:59:59.999999Z").triggeredAudits
    result = advance(client, "2027-01-05T12:00:00Z")
    assert len(result.triggeredAudits) == 1
    audit = repository(client).get_audit(wid, second["versionId"], result.triggeredAudits[0])
    assert audit is not None and audit.started_at == datetime(2027, 1, 5, 12, tzinfo=UTC)
    assert repository(client).get_schedule(wid).nextAuditAt == datetime(2027, 1, 6, tzinfo=UTC)
    with repository(client)._db() as db:
        job = json.loads(
            db.execute("SELECT payload FROM jobs WHERE audit_id=?", (audit.audit_id,)).fetchone()[0]
        )
        assert datetime.fromisoformat(job["queue_deadline"]) - datetime.now(UTC) < timedelta(
            seconds=16
        )
    worker(client)
    assert (
        repository(client).get_audit(wid, second["versionId"], audit.audit_id).completed_at
        >= audit.started_at
    )
    restarted = SQLiteRepository(repository(client).path)
    restarted.initialize()
    assert not restarted.process_schedules(
        datetime(2027, 1, 5, 12, tzinfo=UTC), session_id="s", owner_subject="admin"
    ).triggeredAudits
    assert not lifecycle(client).tick().triggeredAudits  # Real ticks exclude test namespaces.


def test_reminder_snapshot_suppression_dedup_deadline_changes(client: TestClient) -> None:
    accepted = upload(client)
    wid = accepted["workbookId"]
    worker(client)
    policy(client, wid, {"enabled": True, "intervalDays": 1, "complianceDeadline": "2027-04-26"})
    result = advance(client, "2027-01-26T00:00:00Z")
    assert (
        result.triggeredAudits and result.triggeredNotifications == []
    )  # pre-dispatch clean snapshot
    worker(client)
    upload(client, wid=wid, clean=False)
    worker(client)
    result = advance(client, "2027-04-20T00:00:00Z")
    assert len(result.triggeredNotifications) == 2  # Suppressed 90-day key remains considered.
    worker(client)
    result = advance(client, "2027-04-20T00:00:00Z")
    assert result.triggeredNotifications == result.triggeredAudits == []
    records = client.get(f"/api/v1/workbooks/{wid}/notifications", headers=headers()).json()[
        "notifications"
    ]
    assert [r["scheduledFor"] for r in records] == ["2027-03-27T00:00:00Z", "2027-04-19T00:00:00Z"]
    assert all(r["status"] == "DELIVERED" for r in records)
    policy(client, wid, {"enabled": False, "complianceDeadline": "2027-04-27"})
    policy(client, wid, {"enabled": False, "complianceDeadline": "2027-04-26"})
    assert not advance(client, "2027-04-21T00:00:00Z").triggeredNotifications
    assert (
        client.get(f"/api/v1/workbooks/{wid}/notifications", headers=headers()).json()[
            "notifications"
        ]
        == records
    )


def test_leap_year_exact_threshold_and_no_backfill(client: TestClient) -> None:
    advance(client, "2028-01-27T00:00:00Z")
    item = upload(client, clean=False)
    worker(client)
    policy(client, item["workbookId"], {"enabled": False, "complianceDeadline": "2028-04-26"})
    assert len(advance(client, "2028-01-27T00:00:00Z").triggeredNotifications) == 1
    assert len(advance(client, "2028-04-19T00:00:00Z").triggeredNotifications) == 2
    advance(client, "2028-04-20T00:00:00Z")
    late = upload(client, clean=False)
    worker(client)
    policy(client, late["workbookId"], {"enabled": False, "complianceDeadline": "2028-04-26"})
    assert not advance(client, "2028-04-20T00:00:00Z").triggeredNotifications


def test_transaction_rollback_concurrent_claims_and_lost_ack(client: TestClient) -> None:
    item = upload(client)
    worker(client)
    wid = item["workbookId"]
    policy(client, wid, {"enabled": True, "intervalDays": 1})
    with patch.object(SQLiteRepository, "_order", side_effect=StorageError("disk failure")):
        with pytest.raises(StorageError):
            advance(client, "2027-01-02T00:00:00Z")
    assert repository(client).effective_now("s") == INITIAL
    assert repository(client).get_schedule(wid).nextAuditAt == datetime(2027, 1, 2, tzinfo=UTC)

    # Concurrent clock calls serialize: one accepts work, the replay returns no new IDs.
    def dispatch() -> str:
        try:
            result = advance(client, "2027-01-02T00:00:00Z")
            return "accepted" if result.triggeredAudits else "empty"
        except ProjectException as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: dispatch(), range(2)))
    assert results.count("accepted") == 1
    assert set(results) <= {"accepted", "empty"}
    worker(client)
    assert not advance(client, "2027-01-02T00:00:00Z").triggeredAudits


@pytest.mark.parametrize(
    "token,get_status,put_status",
    [
        ("owner", 200, 200),
        ("reviewer", 200, 403),
        ("ungranted", 403, 403),
        ("foreign", 403, 403),
        ("admin", 403, 403),
        ("wrong", 401, 401),
    ],
)
def test_authorization_before_body(
    client: TestClient, token: str, get_status: int, put_status: int
) -> None:
    item = upload(client)
    path = f"/api/v1/workbooks/{item['workbookId']}"
    for suffix in ("/schedule", "/notifications"):
        assert client.get(path + suffix, headers=headers(token)).status_code == get_status
    assert (
        client.put(path + "/schedule", json={"enabled": False}, headers=headers(token)).status_code
        == put_status
    )
    if token != "owner":
        assert (
            client.put(path + "/schedule", content="invalid", headers=headers(token)).status_code
            == put_status
        )
    assert client.get(path + "/schedule", headers=headers(token, None)).status_code == (
        401 if token == "wrong" else 403 if token == "admin" else 404
    )


@pytest.mark.parametrize(
    "fault,code",
    [
        ("scheduled_audit_failures", "TEST_SCHEDULED_AUDIT_FAILURE"),
        ("report_failures", "TEST_REPORT_FAILURE"),
        ("audit_failures", "TEST_AUDIT_FAILURE"),
    ],
)
def test_durable_audit_faults(client: TestClient, fault: str, code: str) -> None:
    prepare(client, fault, {"faults": {fault: 1}})
    item = upload(client, session=fault)
    worker(client)
    wid, vid = item["workbookId"], item["versionId"]
    aid = item["auditId"]
    if fault == "scheduled_audit_failures":
        policy(client, wid, {"enabled": True, "intervalDays": 1}, fault)
        aid = advance(client, "2027-01-02T00:00:00Z", fault).triggeredAudits[0]
        worker(client)
    audit = repository(client).get_audit(wid, vid, aid)
    assert audit.status == "PROCESSING_FAILED" and audit.error.code == code
    assert audit.findings == [] and audit.error.details["testOnly"] is True
    assert len(audit.checks_performed) == (6 if fault == "report_failures" else 0)
    assert json.loads(repository(client).get_report(aid))["error"]["code"] == code
    assert repository(client).get_version(wid, vid) is not None


def test_notification_storage_and_predelete_faults(client: TestClient) -> None:
    prepare(
        client,
        "faults",
        {"faults": {"storage_failures": 1, "notification_failures": 1, "deletion_failures": 1}},
    )
    bad = client.post(
        "/api/v1/workbooks",
        files={"file": ("bad.xlsx", b"invalid")},
        headers=headers(session="faults"),
    )
    assert bad.status_code == 422  # Does not consume storage fault.
    data = Workbook([Sheet("Sheet1")]).build()
    failed = client.post(
        "/api/v1/workbooks",
        files={"file": ("budget.xlsx", data)},
        headers=headers(session="faults"),
    )
    assert failed.status_code == 500 and failed.json()["code"] == "TEST_STORAGE_FAILURE"
    item = upload(client, session="faults", clean=False)
    worker(client)
    wid = item["workbookId"]
    policy(client, wid, {"enabled": False, "complianceDeadline": "2027-04-26"}, "faults")
    result = advance(client, "2027-01-26T00:00:00Z", "faults")
    assert len(result.triggeredNotifications) == 1
    assert repository(client).get_notifications(wid).notifications[0].status == "FAILED"
    path = f"/api/v1/workbooks/{wid}"
    failed = client.delete(path, headers=headers(session="faults"))
    assert failed.status_code == 500 and failed.json()["code"] == "TEST_DELETION_FAILURE"
    assert client.get(path + "/history", headers=headers(session="faults")).status_code == 200
    assert client.delete(path, headers=headers(session="faults")).status_code == 204


def test_deletion_failure_restart_backups_and_erasure(client: TestClient, tmp_path: Path) -> None:
    item = upload(client, clean=False)
    wid = item["workbookId"]
    path = f"/api/v1/workbooks/{wid}"
    assert client.delete(path, headers=headers()).status_code == 409
    worker(client)
    policy(client, wid, {"enabled": True, "intervalDays": 90, "complianceDeadline": "2027-04-26"})
    advance(client, "2027-01-26T00:00:00Z")
    backup = tmp_path / "backup.sqlite3"
    with sqlite3.connect(repository(client).path) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    blob_backup = tmp_path / "blob-backup"
    shutil.copytree(client.app.state.services.settings.blob_root, blob_backup)
    settings = client.app.state.services.settings.model_copy(
        update={"managed_metadata_backups": [backup], "managed_blob_backups": [blob_backup]}
    )
    service = LifecycleService(replace(client.app.state.services, settings=settings))
    store = client.app.state.services.blob_store
    actual = store.delete_workbook_sources

    def partial(wid: str, sid: str | None, deadline: float) -> None:
        actual(wid, sid, deadline)
        raise StorageError("lost storage acknowledgment")

    with patch.object(store, "delete_workbook_sources", side_effect=partial):
        with pytest.raises(ProjectException) as failure:
            service.delete_workbook(wid, OWNER, "s")
        assert failure.value.code == "DELETION_FAILED"
    assert client.get(path + "/history", headers=headers()).status_code == 404
    assert client.delete(path, headers=headers("foreign")).status_code == 404
    with pytest.raises(ProjectException) as failure:
        advance(client, "2027-01-27T00:00:00Z")
    assert failure.value.code == "DELETION_IN_PROGRESS"
    restarted = SQLiteRepository(repository(client).path)
    restarted.initialize()
    service = LifecycleService(
        replace(client.app.state.services, repository=restarted, settings=settings)
    )
    service.delete_workbook(wid, OWNER, "s")
    assert client.delete(path, headers=headers()).status_code == 404
    assert list(blob_backup.rglob("source")) == []
    for dbpath in (restarted.path, backup):
        with sqlite3.connect(dbpath) as db:
            for table in (
                "workbooks",
                "versions",
                "audits",
                "reports",
                "jobs",
                "record_order",
                "schedules",
                "notifications",
                "reminder_occurrences",
                "idempotency",
                "upload_staging",
                "remediations",
                "remediation_jobs",
                "outputs",
                "deletions",
            ):
                assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
    assert restarted.effective_now("s") == datetime(2027, 1, 26, tzinfo=UTC)


def test_deletion_storage_fence_and_restore_journal(client: TestClient, tmp_path: Path) -> None:
    item = upload(client)
    worker(client)
    wid = item["workbookId"]
    before = tmp_path / "predelete.sqlite3"
    with sqlite3.connect(repository(client).path) as source, sqlite3.connect(before) as target:
        source.backup(target)
    with workbook_storage_lock(repository(client).path, wid):
        assert client.delete(f"/api/v1/workbooks/{wid}", headers=headers()).status_code == 409
    assert client.delete(f"/api/v1/workbooks/{wid}", headers=headers()).status_code == 204
    # Restore only content DB; the newest content-free journal must be carried forward.
    restored = SQLiteRepository(before)
    shutil.copyfile(
        repository(client).path.with_suffix(".sqlite3.deletions"),
        before.with_suffix(".sqlite3.deletions"),
    )
    restored.initialize()
    assert restored.get_workbook(wid) is None
    assert restored.deletion_owner(wid) == ("owner", "s")


def test_clock_boundaries_and_time_range_rollback(client: TestClient) -> None:
    with pytest.raises(ProjectException) as failure:
        advance(client, "2026-12-31T23:59:59Z")
    assert failure.value.code == "CLOCK_REWIND"
    for value in (
        "2027-01-02T00:00:00+00:00",
        "2027-01-02T00:00:00.1234567Z",
        "2027-02-29T00:00:00Z",
    ):
        with pytest.raises(ProjectException) as failure:
            advance(client, value)
        assert failure.value.code == "INVALID_REQUEST"
    with pytest.raises(ProjectException) as failure:
        lifecycle(client).advance_clock(OWNER, "s", {"currentTime": "2027-01-01T00:00:00Z"})
    assert failure.value.code == "FORBIDDEN"
    advance(client, "9999-12-31T00:00:00Z")
    item = upload(client)
    worker(client)
    result = client.put(
        f"/api/v1/workbooks/{item['workbookId']}/schedule",
        json={"enabled": True, "intervalDays": 1},
        headers=headers(),
    )
    assert result.status_code == 400 and result.json()["code"] == "TIME_RANGE_EXCEEDED"
    assert repository(client).get_schedule(item["workbookId"]).enabled is False


def test_generated_version_following_remediation_fault_and_full_deletion(
    client: TestClient,
) -> None:
    prepare(client, "rem", {"faults": {"remediation_failures": 1}})
    item = upload(client, session="rem", clean=False)
    worker(client)
    wid, vid, aid = (item[key] for key in ("workbookId", "versionId", "auditId"))
    path = f"/api/v1/workbooks/{wid}/versions/{vid}"
    audit = repository(client).get_audit(wid, vid, aid)
    body = {
        "auditId": aid,
        "decisions": [{"findingId": f.findingId, "action": "DECLINE"} for f in audit.findings],
    }
    policy(client, wid, {"enabled": True, "intervalDays": 1}, "rem")
    accepted = client.post(
        path + "/remediations",
        json=body,
        headers=headers(session="rem") | {"Idempotency-Key": "fault"},
    )
    assert accepted.status_code == 202, accepted.text
    worker(client)
    failed = repository(client).get_remediation(accepted.json()["remediationId"])
    assert failed.error.code == "TEST_REMEDIATION_FAILURE" and failed.output_id is None
    accepted = client.post(
        path + "/remediations",
        json=body,
        headers=headers(session="rem") | {"Idempotency-Key": "success"},
    )
    worker(client)
    completed = repository(client).get_remediation(accepted.json()["remediationId"])
    assert completed.status == "REMEDIATED"
    assert repository(client).get_schedule(wid).versionId == completed.output_version_id
    triggered = advance(client, "2027-01-02T00:00:00Z", "rem").triggeredAudits[0]
    worker(client)
    assert repository(client).get_audit(wid, completed.output_version_id, triggered) is not None
    assert (
        client.delete(f"/api/v1/workbooks/{wid}", headers=headers(session="rem")).status_code == 204
    )
    for url in (
        path,
        path + "/source",
        path + f"/audits/{aid}/report",
        f"/api/v1/outputs/{completed.output_id}/workbook",
        f"/api/v1/remediations/{completed.remediation_id}",
        f"/api/v1/workbooks/{wid}/schedule",
        f"/api/v1/workbooks/{wid}/notifications",
    ):
        assert client.get(url, headers=headers(session="rem")).status_code == 404, url
    with repository(client)._db() as db:
        for table in ("remediations", "remediation_jobs", "outputs", "idempotency", "reports"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
    assert not list(client.app.state.services.settings.blob_root.rglob("source"))


def test_clock_batch_limit_rolls_back_and_setup_capabilities(client: TestClient) -> None:
    accepted = upload(client)
    worker(client)
    wid = accepted["workbookId"]
    policy(client, wid, {"enabled": True, "intervalDays": 1})
    repo = repository(client)
    # Arrange 1,001 independent idle workbooks without paying for 1,001 engine runs.
    with repo._db(write=True) as db:
        workbook = json.loads(
            db.execute("SELECT payload FROM workbooks WHERE id=?", (wid,)).fetchone()[0]
        )
        version = json.loads(
            db.execute(
                "SELECT payload FROM versions WHERE id=?", (accepted["versionId"],)
            ).fetchone()[0]
        )
        schedule = json.loads(
            db.execute("SELECT payload FROM schedules WHERE workbook_id=?", (wid,)).fetchone()[0]
        )
        for n in range(1000):
            new_wid, new_vid = f"limit-w-{n}", f"limit-v-{n}"
            db.execute(
                "INSERT INTO workbooks VALUES (?,?,?,?)",
                (new_wid, "s", "owner", json.dumps(workbook | {"workbook_id": new_wid})),
            )
            db.execute(
                "INSERT INTO versions VALUES (?,?,?,?)",
                (
                    new_vid,
                    new_wid,
                    f"tests/s/{new_wid}/{new_vid}/source",
                    json.dumps(
                        version
                        | {
                            "workbook_id": new_wid,
                            "version_id": new_vid,
                            "blob_key": f"tests/s/{new_wid}/{new_vid}/source",
                        }
                    ),
                ),
            )
            db.execute(
                "INSERT INTO record_order(workbook_id,kind,record_id) VALUES (?,?,?)",
                (new_wid, "version", new_vid),
            )
            db.execute(
                "INSERT INTO schedules VALUES (?,?,?,?)",
                (
                    new_wid,
                    "s",
                    json.dumps(schedule | {"workbookId": new_wid, "versionId": new_vid}),
                    "2027-01-01T00:00:00+00:00",
                ),
            )
    with pytest.raises(ProjectException) as error:
        advance(client, "2027-01-02T00:00:00Z")
    assert error.value.code == "CLOCK_WORK_LIMIT"
    assert repo.effective_now("s") == INITIAL
    with repo._db() as db:
        assert db.execute("SELECT count(*) FROM audits").fetchone()[0] == 1
    for setup in (
        {"clock": {"currentTime": "2027-01-01T00:00:00+00:00"}},
        {"faults": {"report_failures": 1, "audit_failures": 1}},
        {"faults": {"notification_failures": True}},
        {"faults": {}},
        {"faults": {"unknown": 1}},
    ):
        result = client.post(
            "/test/setup",
            json={"session_id": "bad", "setup": setup},
            headers=headers("admin", None),
        )
        assert result.status_code == 400 and set(result.json()) == {"message", "source"}
    assert repo.get_test_session("bad") is None


def test_clean_suppression_failed_eligibility_and_namespace_cleanup(client: TestClient) -> None:
    item = upload(client)
    worker(client)
    policy(client, item["workbookId"], {"enabled": False, "complianceDeadline": "2027-04-26"})
    prepare(client, "failed", {"faults": {"audit_failures": 1}})
    bad = upload(client, session="failed")
    worker(client)
    policy(
        client, bad["workbookId"], {"enabled": False, "complianceDeadline": "2027-04-26"}, "failed"
    )
    assert len(advance(client, "2027-01-26T00:00:00Z", "failed").triggeredNotifications) == 1
    assert repository(client).effective_now("s") == INITIAL
    assert not advance(client, "2027-01-26T00:00:00Z").triggeredNotifications
    # Namespace cleanup removes clock/fault state and all scheduling children.
    lifecycle(client).services.session_service.cleanup("failed", ADMIN)
    assert repository(client).get_test_session("failed").clock_current_time is None
    with repository(client)._db() as db:
        for table in ("schedules", "notifications", "reminder_occurrences", "deletions"):
            assert not db.execute(f"SELECT 1 FROM {table} WHERE session_id='failed'").fetchone()
    assert repository(client).get_workbook(item["workbookId"]) is not None


def test_s3_versioned_cleanup_uses_exact_prefix_and_checks_errors() -> None:
    import time
    from unittest.mock import Mock

    from app.storage.blobs import S3BlobStore

    client = Mock()
    prefix = "sources/ordinary/w/"
    client.get_paginator.return_value.paginate.side_effect = [
        [
            {
                "Versions": [
                    {"Key": prefix + "v/source", "VersionId": "v1"},
                    {"Key": prefix + "v/source", "VersionId": "v0"},
                ],
                "DeleteMarkers": [{"Key": prefix + "v/source", "VersionId": "marker"}],
            }
        ],
        [{}],
    ]
    client.delete_objects.return_value = {}
    store = S3BlobStore("bucket", "us-east-2", client=client)
    store.delete_workbook_sources("w", None, time.monotonic() + 25)
    deleted = client.delete_objects.call_args.kwargs["Delete"]["Objects"]
    assert {item["VersionId"] for item in deleted} == {"v1", "v0", "marker"}
    client.get_paginator.return_value.paginate.assert_called_with(Bucket="bucket", Prefix=prefix)
    client.get_paginator.return_value.paginate.side_effect = [
        [{"Versions": [{"Key": prefix + "v/source", "VersionId": "v1"}]}]
    ]
    client.delete_objects.return_value = {"Errors": [{"Code": "AccessDenied"}]}
    with pytest.raises(StorageError, match="incomplete"):
        store.delete_workbook_sources("w", None, time.monotonic() + 25)


def test_real_scheduler_processes_claim_once_and_drain(client: TestClient, tmp_path: Path) -> None:
    import os
    import signal
    import subprocess
    import sys
    import time

    item = upload(client, session=None)
    worker(client)
    wid = item["workbookId"]
    policy(client, wid, {"enabled": True, "intervalDays": 1}, None)
    with repository(client)._db(write=True) as db:
        row = json.loads(
            db.execute("SELECT payload FROM schedules WHERE workbook_id=?", (wid,)).fetchone()[0]
        )
        row["nextAuditAt"] = (
            (datetime.now(UTC) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        )
        db.execute("UPDATE schedules SET payload=? WHERE workbook_id=?", (json.dumps(row), wid))
    env = os.environ | {
        "METADATA_DB_PATH": str(repository(client).path),
        "BLOB_ROOT": str(client.app.state.services.settings.blob_root),
        "APP_ENV": "development",
        "BLOB_BACKEND": "filesystem",
    }
    logs = [tmp_path / f"worker-{number}.log" for number in range(2)]
    handles = [path.open("w") for path in logs]
    processes = [
        subprocess.Popen(
            [sys.executable, "-m", "app.jobs.worker", "--concurrency", "1"],
            env=env,
            stdout=handle,
            stderr=handle,
        )
        for handle in handles
    ]
    started = time.monotonic()
    try:
        while time.monotonic() - started < 10:
            _, audits = repository(client).history_records(wid)
            if len(audits) == 2 and audits[-1].status != "PROCESSING":
                break
            time.sleep(0.05)
        assert len(audits) == 2 and audits[-1].status == "AUDIT_COMPLETE"
        assert time.monotonic() - started < 5  # Real scheduling cadence, not simulated months.
    finally:
        for process in processes:
            process.send_signal(signal.SIGTERM)
        for process in processes:
            assert process.wait(timeout=15) == 0
        for handle in handles:
            handle.close()
    events = [
        json.loads(line)
        for path in logs
        for line in path.read_text().splitlines()
        if line.startswith("{")
    ]
    assert sum(e["event"] == "scheduled_audit_accepted" for e in events) == 1
    assert sum(e["event"] == "worker_stopped" and e.get("code") == "DRAINED" for e in events) == 2
    assert any(e["event"] == "backend_monitor" and e["queueDepth"] >= 0 for e in events)


def test_clock_capability_ownership_legacy_and_late_transaction_failure(client: TestClient) -> None:
    from app.test_interface.service import PersistentSessionService

    current = client.app.state.services
    disabled = current.settings.model_copy(
        update={"enable_test_clock": False, "enable_test_audit_faults": False}
    )
    sessions = PersistentSessionService(repository(client), current.blob_store, disabled)
    for setup in (
        {"clock": {"currentTime": "2027-01-01T00:00:00Z"}},
        {"faults": {"deletion_failures": 1}},
    ):
        with pytest.raises(ProjectException) as error:
            sessions.prepare("disabled", ADMIN, setup)
        assert error.value.status_code == 403
    assert repository(client).get_test_session("disabled") is None
    sessions.prepare("legacy", ADMIN, {})
    with pytest.raises(ProjectException) as error:
        advance(client, "2027-01-01T00:00:00Z", "legacy")
    assert error.value.code == "LEGACY_SESSION_CLOCK"
    for sid, principal, expected in (
        (None, ADMIN, "TEST_CLOCK_DISABLED"),
        ("unknown", ADMIN, "INVALID_TEST_SESSION"),
        ("s", Principal(subject="other-admin", role="test_admin"), "FORBIDDEN"),
    ):
        with pytest.raises(ProjectException) as error:
            lifecycle(client).authorize_clock(principal, sid)
        assert error.value.code == expected
    item = upload(client, clean=False)
    worker(client)
    wid = item["workbookId"]
    policy(client, wid, {"enabled": False, "complianceDeadline": "2027-04-26"})
    # Fail at the final time write, after eligible notification + dedup rows were inserted.
    with patch.object(
        SQLiteRepository, "_save_session", side_effect=StorageError("commit failure")
    ):
        with pytest.raises(StorageError):
            advance(client, "2027-01-26T00:00:00Z")
    assert repository(client).effective_now("s") == INITIAL
    assert repository(client).get_notifications(wid).notifications == []
    with repository(client)._db() as db:
        assert db.execute("SELECT count(*) FROM reminder_occurrences").fetchone()[0] == 0
    assert len(advance(client, "2027-01-26T00:00:00Z").triggeredNotifications) == 1


def test_additive_migration_keeps_retained_report_bytes(client: TestClient) -> None:
    item = upload(client)
    worker(client)
    repo = repository(client)
    original = json.loads(repo.get_report(item["auditId"]))
    original["specificationVersion"] = "2.1.0"
    retained = json.dumps(original, indent=2).encode()
    with repo._db(write=True) as db:
        # Arrange a saved Phase 4 snapshot and schema-2 database before reopening.
        db.execute("DELETE FROM reports WHERE audit_id=?", (item["auditId"],))
        db.execute("INSERT INTO reports VALUES (?,?)", (item["auditId"], retained))
        for table in (
            "schedules",
            "reminder_occurrences",
            "notifications",
            "deletions",
            "deleted_workbooks",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute("PRAGMA user_version=2")
    repo.initialize()
    repo.initialize()
    assert repo.get_report(item["auditId"]) == retained
    assert repo.get_version(item["workbookId"], item["versionId"]) is not None
    assert repo.get_schedule(item["workbookId"]).enabled is False


def test_clock_activity_protects_external_evaluation_using_real_time(client: TestClient) -> None:
    repo = repository(client)
    with repo._db(write=True) as db:
        session = repo._session(db, "s")
        repo._save_session(
            db,
            session.model_copy(
                update={"deployment_protected_until": datetime.now(UTC) + timedelta(seconds=1)}
            ),
        )
    before = datetime.now(UTC)
    advance(client, "2027-06-01T00:00:00Z")
    session = repo.get_test_session("s")
    assert (
        before + timedelta(seconds=2700)
        <= session.deployment_protected_until
        < before + timedelta(seconds=2705)
    )


def test_corrupt_deletion_journal_fails_closed_on_restore(client: TestClient) -> None:
    item = upload(client)
    worker(client)
    repo = SQLiteRepository(repository(client).path)
    journal = repo.path.with_suffix(repo.path.suffix + ".deletions")
    journal.write_bytes(b"corrupt journal")
    with pytest.raises(StorageError):
        repo.initialize()
    assert not repo.health()
    with pytest.raises(StorageError):
        repo.get_workbook(item["workbookId"])


def test_deletion_compaction_failure_retains_owner_recovery(client: TestClient) -> None:
    item = upload(client)
    worker(client)
    wid = item["workbookId"]
    repo = repository(client)
    with patch.object(repo, "compact_deleted_content", side_effect=StorageError("checkpoint busy")):
        with pytest.raises(ProjectException) as error:
            lifecycle(client).delete_workbook(wid, OWNER, "s")
        assert error.value.code == "DELETION_FAILED"
    assert repo.get_workbook(wid) is None
    assert repo.deletion_owner(wid) == ("owner", "s")
    lifecycle(client).delete_workbook(wid, OWNER, "s")
    assert repo.deletion_owner(wid) is None
