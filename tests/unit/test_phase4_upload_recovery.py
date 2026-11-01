"""Upload acknowledgement and crash recovery using real SQLite and filesystem writes.

Audit/editor behavior uses the explicitly synthetic Phase 4 engine fixture. Only
focused boundary fault injection is mocked; database commits and blob I/O are real.
"""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from app.contracts.errors import StorageError
from app.contracts.models import AuditRecord, JobRecord, VersionRecord, WorkbookRecord
from app.jobs.worker import AuditWorker
from app.storage.blobs import sha256
from app.storage.sqlite import SQLiteRepository
from tests.unit.test_phase4_routes import (
    workflow_auth,
    workflow_tick,
)
from tests.unit.test_phase4_routes import (
    workflow_client as workflow_client,
)
from tests.unit.test_phase4_worker import engine_fixture


def counts(client: TestClient) -> list[int]:
    with sqlite3.connect(client.app.state.services.settings.metadata_db_path) as db:
        return [
            db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("workbooks", "versions", "audits", "jobs")
        ]


def post_source(
    client: TestClient, name: str = "source.xlsx", data: bytes | None = None
) -> Response:
    original, _, _, _ = engine_fixture()
    response: Response = client.post(
        "/api/v1/workbooks",
        files={"file": (name, original if data is None else data)},
        headers=workflow_auth(),
    )
    return response


def test_lost_put_acknowledgement_deletes_uncommitted_blob(
    workflow_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = workflow_client
    services = client.app.state.services
    put_source = services.blob_store.put_source
    attempted: list[str] = []

    def lost_ack(key: str, data: bytes) -> str:
        put_source(key, data)
        attempted.append(key)
        raise StorageError("Simulated lost blob acknowledgement")

    monkeypatch.setattr(services.blob_store, "put_source", lost_ack)
    response = post_source(client)
    assert response.status_code == 500
    assert response.json()["code"] == "STORAGE_FAILED"
    assert counts(client) == [0, 0, 0, 0]
    assert len(attempted) == 1
    assert not (services.settings.blob_root / attempted[0]).exists()
    # The expired staging marker survives, so interrupted compensation remains recoverable.
    assert services.repository.pending_blob_cleanup()[0][0] == attempted[0]


def test_lost_metadata_acknowledgement_recovers_acceptance_without_deleting_source(
    workflow_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = workflow_client
    services = client.app.state.services
    create_upload = services.repository.create_upload

    def lost_ack(
        workbook: WorkbookRecord, version: VersionRecord, audit: AuditRecord, job: JobRecord
    ) -> None:
        create_upload(workbook, version, audit, job)
        raise StorageError("Simulated lost metadata commit acknowledgement")

    monkeypatch.setattr(services.repository, "create_upload", lost_ack)
    response = post_source(client)
    assert response.status_code == 202, response.text
    assert counts(client) == [1, 1, 1, 1]
    accepted = response.json()
    path = f"/api/v1/workbooks/{accepted['workbookId']}/versions/{accepted['versionId']}"
    original, _, _, _ = engine_fixture()
    assert client.get(path + "/source", headers=workflow_auth()).content == original
    assert services.repository.pending_blob_cleanup() == []
    workflow_tick(client)
    assert (
        client.get(path + "/audits/" + accepted["auditId"], headers=workflow_auth()).json()[
            "status"
        ]
        == "REVIEW_REQUIRED"
    )


def test_uncertain_metadata_confirmation_preserves_committed_source(
    workflow_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = workflow_client
    services = client.app.state.services
    create_upload = services.repository.create_upload
    get_version = services.repository.get_version
    committed: list[VersionRecord] = []

    def lost_ack(
        workbook: WorkbookRecord, version: VersionRecord, audit: AuditRecord, job: JobRecord
    ) -> None:
        create_upload(workbook, version, audit, job)
        committed.append(version)
        raise StorageError("Simulated lost metadata commit acknowledgement")

    def unavailable(*args: object) -> None:
        raise StorageError("Confirmation temporarily unavailable")

    monkeypatch.setattr(services.repository, "create_upload", lost_ack)
    monkeypatch.setattr(services.repository, "get_version", unavailable)
    response = post_source(client)
    assert response.status_code == 500
    assert response.json()["code"] == "STORAGE_FAILED"
    assert counts(client) == [1, 1, 1, 1]
    version = committed[0]
    assert get_version(version.workbook_id, version.version_id) is not None
    assert sha256(services.blob_store.read_source(version.blob_key)) == version.source_sha256
    assert services.repository.pending_blob_cleanup() == []
    monkeypatch.setattr(services.repository, "get_version", get_version)
    workflow_tick(client)
    path = f"/api/v1/workbooks/{version.workbook_id}/versions/{version.version_id}"
    assert client.get(path + "/source", headers=workflow_auth()).status_code == 200


def test_expired_upload_staging_is_cleaned_after_restart_and_fences_late_acceptance(
    workflow_client: TestClient,
) -> None:
    client = workflow_client
    services = client.app.state.services
    data, validated, template, _ = engine_fixture()
    now = datetime.now(UTC)
    workbook = WorkbookRecord(
        workbook_id="crash-workbook",
        owner_subject="owner",
        session_id=None,
        created_at=now,
    )
    audit = template.model_copy(
        update={
            "audit_id": "crash-audit",
            "workbook_id": workbook.workbook_id,
            "version_id": "crash-version",
            "status": "PROCESSING",
            "started_at": now,
            "completed_at": None,
            "findings": [],
            "limitations": [],
            "review_context": None,
            "checks_performed": None,
        }
    )
    version = VersionRecord(
        version_id="crash-version",
        workbook_id=workbook.workbook_id,
        owner_subject="owner",
        session_id=None,
        file_name="source.xlsx",
        source_sha256=sha256(data),
        blob_key="ordinary/crash-workbook/crash-version/source",
        size_bytes=len(data),
        worksheet_count=validated.worksheet_count,
        nonempty_cell_count=validated.nonempty_cell_count,
        created_at=now,
        status="PROCESSING",
        latest_audit_id=audit.audit_id,
        source_version_id=None,
        validated=validated,
    )
    job = JobRecord(
        job_id="crash-job",
        audit_id=audit.audit_id,
        workbook_id=workbook.workbook_id,
        version_id=version.version_id,
        session_id=None,
        state="QUEUED",
        created_at=now,
        queue_deadline=now + timedelta(seconds=15),
        worker_id=None,
        lease_expires_at=None,
    )
    services.repository.reserve_upload_blob(version, now + timedelta(seconds=30))
    services.blob_store.put_source(version.blob_key, data)
    assert services.repository.pending_blob_cleanup() == []
    # Simulate a process dying between object write and metadata acceptance, then
    # advance its durable storage reservation without a timing-dependent sleep.
    with sqlite3.connect(services.settings.metadata_db_path) as db:
        db.execute(
            "UPDATE upload_staging SET deadline=? WHERE version_id=?",
            ((now - timedelta(seconds=1)).isoformat(), version.version_id),
        )
    restarted = SQLiteRepository(services.settings.metadata_db_path)
    restarted.initialize()
    assert restarted.pending_blob_cleanup() == [(version.blob_key, version.source_sha256)]
    assert not AuditWorker(replace(services, repository=restarted)).run_once()
    assert not (services.settings.blob_root / version.blob_key).exists()
    with pytest.raises(StorageError):
        restarted.create_upload(workbook, version, audit, job)
    assert counts(client) == [0, 0, 0, 0]


@pytest.mark.parametrize(
    "name,data,status",
    [
        ("macro.xlsm", b"invalid", 422),
        ("corrupt.xlsx", b"invalid", 422),
        ("source.csv", b"invalid", 415),
    ],
)
def test_rejected_intake_has_no_storage_reservation_or_accepted_metadata(
    workflow_client: TestClient,
    name: str,
    data: bytes,
    status: int,
) -> None:
    client = workflow_client
    response = post_source(client, name, data)
    assert response.status_code == status
    assert counts(client) == [0, 0, 0, 0]
    with sqlite3.connect(client.app.state.services.settings.metadata_db_path) as db:
        assert db.execute("SELECT count(*) FROM upload_staging").fetchone()[0] == 0
    assert not list(client.app.state.services.settings.blob_root.rglob("source"))


def test_failed_compensation_retains_a_durable_marker_for_restart_cleanup(
    workflow_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = workflow_client
    services = client.app.state.services
    put_source = services.blob_store.put_source
    delete_source = services.blob_store.delete_uncommitted_source
    attempted: list[str] = []

    def lost_ack(key: str, data: bytes) -> str:
        put_source(key, data)
        attempted.append(key)
        raise StorageError("Simulated lost blob acknowledgement")

    def cleanup_unavailable(key: str, digest: str) -> None:
        raise StorageError("Temporary cleanup outage")

    monkeypatch.setattr(services.blob_store, "put_source", lost_ack)
    monkeypatch.setattr(services.blob_store, "delete_uncommitted_source", cleanup_unavailable)
    response = post_source(client)
    assert response.status_code == 500
    assert counts(client) == [0, 0, 0, 0]
    assert (services.settings.blob_root / attempted[0]).exists()
    assert services.repository.pending_blob_cleanup()[0][0] == attempted[0]
    monkeypatch.setattr(services.blob_store, "delete_uncommitted_source", delete_source)
    restarted = SQLiteRepository(services.settings.metadata_db_path)
    restarted.initialize()
    assert not AuditWorker(replace(services, repository=restarted)).run_once()
    assert not (services.settings.blob_root / attempted[0]).exists()


def test_upload_queue_budget_begins_after_storage_acknowledgement(
    workflow_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = workflow_client
    services = client.app.state.services
    put_source = services.blob_store.put_source
    started = datetime.now(UTC)
    elapsed = timedelta()

    class UploadClock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return started + elapsed

    def delayed_storage(key: str, data: bytes) -> str:
        nonlocal elapsed
        digest = put_source(key, data)
        # Advance only the upload coordinator's clock, avoiding a slow/flaky sleep.
        elapsed = timedelta(seconds=20)
        return digest

    monkeypatch.setattr("app.services.projects.datetime", UploadClock)
    monkeypatch.setattr(services.blob_store, "put_source", delayed_storage)
    response = post_source(client)
    assert response.status_code == 202, response.text
    with sqlite3.connect(services.settings.metadata_db_path) as db:
        row = db.execute("SELECT payload FROM jobs").fetchone()
    job = JobRecord.model_validate_json(row[0])
    assert job.created_at == started + elapsed
    assert job.queue_deadline == started + elapsed + timedelta(seconds=15)
    accepted = response.json()
    version = services.repository.get_version(accepted["workbookId"], accepted["versionId"])
    audit = services.repository.get_audit(
        accepted["workbookId"], accepted["versionId"], accepted["auditId"]
    )
    assert version.created_at == audit.started_at == job.created_at
