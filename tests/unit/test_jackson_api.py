"""HTTP wire, authorization, bounded intake and storage faults with isolated adapters."""

import hashlib
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.auth.service import TokenAuthService
from app.config import SystemSettings
from app.contracts.errors import StorageError
from app.contracts.models import Principal, TestSession
from app.contracts.ports import SystemServices
from app.jobs.worker import AuditWorker
from app.main import create_app
from app.storage.blobs import FilesystemBlobStore
from app.storage.sqlite import SQLiteRepository
from tests.unit.jackson_support import TestEngine, TestRunner


@pytest.fixture
def services(tmp_path: Path) -> SystemServices:
    tokens = {
        hashlib.sha256(token.encode()).hexdigest(): Principal(subject=subject, role=role)
        for token, subject, role in (
            ("a", "A", "user"),
            ("b", "B", "user"),
            ("r", "R", "reviewer"),
            ("u", "U", "reviewer"),
            ("t", "T", "test_admin"),
        )
    }
    settings = SystemSettings(
        _env_file=None,
        ruleset_version="1.0.0",  # Exercise the retained Phase 3 adapter contract explicitly.
        metadata_db_path=tmp_path / "metadata.sqlite3",
        blob_root=tmp_path / "sources",
        SYSTEM_TOKEN_HASHES_JSON=tokens,
        REVIEWER_GRANTS_JSON={"R": ["A"]},
        audit_timeout_seconds=2,
    )
    return SystemServices(
        settings=settings,
        repository=SQLiteRepository(settings.metadata_db_path),
        blob_store=FilesystemBlobStore(settings.blob_root),
        auth_service=TokenAuthService(settings),
        engine=TestEngine(),
        runner=TestRunner(),
    )


@pytest.fixture
def client(services: SystemServices) -> Iterator[TestClient]:
    with TestClient(create_app(services=services)) as active:
        yield active


def auth(token: str = "a") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def upload(
    client: TestClient,
    filename: str = "budget.xlsx",
    data: bytes = b"test-only-source",
    headers: dict[str, str] | None = None,
) -> object:
    return client.post(
        "/api/v1/workbooks", files={"file": (filename, data)}, headers=headers or auth()
    )


def paths(body: dict[str, str]) -> tuple[str, str]:
    version = f"/api/v1/workbooks/{body['workbookId']}/versions/{body['versionId']}"
    return version, f"{version}/audits/{body['auditId']}"


def counts(services: SystemServices) -> list[int]:
    with sqlite3.connect(services.settings.metadata_db_path) as db:
        return [
            db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("workbooks", "versions", "audits", "jobs")
        ]


def test_acceptance_and_durable_completion(client: TestClient, services: SystemServices) -> None:
    assert client.get("/api/v1/health").json() == {
        "status": "AVAILABLE",
        "apiVersion": "1.0",
        "rulesetVersion": "1.0.0",
    }
    response = upload(client)
    assert response.status_code == 202
    assert set(response.json()) == {"workbookId", "versionId", "auditId", "status"}
    assert response.json()["status"] == "PROCESSING"
    vp, ap = paths(response.json())
    assert client.get(ap, headers=auth()).json()["completedAt"] is None
    # A new repository and app read durable metadata; a separate child executes the audit.
    restarted = replace(services, repository=SQLiteRepository(services.settings.metadata_db_path))
    assert AuditWorker(restarted).run_once()
    assert not AuditWorker(restarted).run_once()
    with TestClient(create_app(services=restarted)) as other:
        result = other.get(ap, headers=auth()).json()
        assert result["status"] == "AUDIT_COMPLETE"
        assert result["findings"] == [] and result["error"] is None
        assert result["completedAt"].endswith("Z") and result["startedAt"].endswith("Z")
        version = other.get(vp, headers=auth()).json()
        assert version["sha256"] == hashlib.sha256(b"test-only-source").hexdigest()
        assert version["status"] == "AUDIT_COMPLETE"
        assert version["sourceVersionId"] is None
    assert counts(services) == [1, 1, 1, 1]


@pytest.mark.parametrize("token,status", [(None, 401), ("wrong", 401), ("t", 403)])
def test_project_auth(client: TestClient, token: str | None, status: int) -> None:
    for path in ("/api/v1/workbooks/x/versions/y", "/api/v1/workbooks/x/versions/y/audits/z"):
        response = client.get(path, headers=auth(token) if token else {})
        assert response.status_code == status
        assert set(response.json()) == {"code", "message", "requestId", "details"}
        assert response.json()["requestId"] == response.headers["X-Request-ID"]
    response = client.post("/api/v1/workbooks", headers=auth(token) if token else {})
    assert response.status_code == status


def test_ownership_and_reviewer_grants(client: TestClient, services: SystemServices) -> None:
    vp, ap = paths(upload(client).json())
    original = services.blob_store.read_source
    services.blob_store.read_source = Mock(side_effect=AssertionError("must authorize first"))
    for token in ("b", "u"):
        assert client.get(vp, headers=auth(token)).status_code == 403
        assert client.get(ap, headers=auth(token)).status_code == 403
    services.blob_store.read_source.assert_not_called()
    services.blob_store.read_source = original
    assert client.get(vp, headers=auth("r")).status_code == 200
    assert client.get(ap, headers=auth("r")).status_code == 200
    assert upload(client, headers=auth("r")).status_code == 403
    assert (
        client.get(vp.replace("/versions/", "/versions/wrong"), headers=auth()).status_code == 404
    )
    assert client.get(ap + "wrong", headers=auth()).status_code == 404
    assert (
        client.get(vp.replace("workbooks/", "workbooks/wrong"), headers=auth()).status_code == 404
    )


@pytest.mark.parametrize(
    "filename,status,code",
    [
        ("book.xls", 415, "UNSUPPORTED_FILE_TYPE"),
        ("book.csv", 415, "UNSUPPORTED_FILE_TYPE"),
        ("book", 415, "UNSUPPORTED_FILE_TYPE"),
        ("book.xlsm", 422, "MACRO_ENABLED_WORKBOOK_UNSUPPORTED"),
        ("x" * 256 + ".xlsx", 400, "INVALID_FILENAME"),
    ],
)
def test_filename_rejections(
    client: TestClient, services: SystemServices, filename: str, status: int, code: str
) -> None:
    response = upload(client, filename=filename)
    assert response.status_code == status and response.json()["code"] == code
    assert counts(services) == [0, 0, 0, 0]
    assert not list(services.settings.blob_root.rglob("source"))


@pytest.mark.parametrize(
    "mode,status", [("reject", 422), ("bad_hash", 500), ("bad_count", 500), ("bad_parts", 500)]
)
def test_engine_rejections(services: SystemServices, mode: str, status: int) -> None:
    services = replace(services, runner=TestRunner(mode))
    with TestClient(create_app(services=services)) as client:
        assert upload(client).status_code == status
    assert counts(services) == [0, 0, 0, 0]
    assert not list(services.settings.blob_root.rglob("source"))


@pytest.mark.parametrize("case", ["missing", "wrong_field", "text", "truncated", "duplicate"])
def test_malformed_multipart(client: TestClient, services: SystemServices, case: str) -> None:
    if case == "missing":
        response = client.post("/api/v1/workbooks", headers=auth())
    elif case == "wrong_field":
        response = client.post(
            "/api/v1/workbooks", files={"other": ("x.xlsx", b"data")}, headers=auth()
        )
    elif case == "text":
        response = client.post("/api/v1/workbooks", data={"file": "x.xlsx"}, headers=auth())
    elif case == "duplicate":
        response = client.post(
            "/api/v1/workbooks",
            files=[("file", ("a.xlsx", b"a")), ("file", ("b.xlsx", b"b"))],
            headers=auth(),
        )
    else:
        response = client.post(
            "/api/v1/workbooks",
            content=b"--broken\r\n",
            headers=auth() | {"Content-Type": "multipart/form-data; boundary=x"},
        )
    assert response.status_code == 400
    assert counts(services) == [0, 0, 0, 0]


def test_upload_byte_boundary(client: TestClient, services: SystemServices) -> None:
    response = upload(client, data=b"x" * 25_000_000)
    assert response.status_code == 202
    response = upload(client, data=b"x" * 25_000_001)
    assert response.status_code == 413
    assert counts(services) == [1, 1, 1, 1]


def test_source_verification_and_retained_failed_source(
    client: TestClient, services: SystemServices
) -> None:
    vp, ap = paths(upload(client).json())
    source = next(services.settings.blob_root.rglob("source"))
    source.write_bytes(b"tampered")
    assert client.get(vp, headers=auth()).json()["code"] == "SOURCE_INTEGRITY_FAILED"
    assert AuditWorker(services).run_once()
    result = client.get(ap, headers=auth()).json()
    assert result["status"] == "PROCESSING_FAILED"
    assert result["error"]["code"] == "SOURCE_INTEGRITY_FAILED"
    assert result["findings"] == [] and source.exists()


def test_completed_history_is_not_rewritten_by_get_integrity(
    client: TestClient, services: SystemServices
) -> None:
    vp, ap = paths(upload(client).json())
    AuditWorker(services).run_once()
    before = client.get(ap, headers=auth()).json()
    next(services.settings.blob_root.rglob("source")).write_bytes(b"tampered")
    assert client.get(vp, headers=auth()).status_code == 500
    assert client.get(ap, headers=auth()).json() == before


def test_metadata_failure_compensates_blob(client: TestClient, services: SystemServices) -> None:
    services.repository.create_upload = Mock(side_effect=StorageError("private-detail"))
    response = upload(client)
    assert response.status_code == 500 and "private-detail" not in response.text
    assert not list(services.settings.blob_root.rglob("source"))
    assert counts(services) == [0, 0, 0, 0]


def test_storage_failure_and_health(client: TestClient, services: SystemServices) -> None:
    services.blob_store.put_source = Mock(side_effect=StorageError("private"))
    services.blob_store.delete_uncommitted_source = Mock()
    assert upload(client).status_code == 500
    # A failed put may have lost its acknowledgement. Staging permits safe
    # hash-checked compensation while preserving an immutable deletion intent.
    services.blob_store.delete_uncommitted_source.assert_called_once()
    assert counts(services) == [0, 0, 0, 0]
    assert not list(services.settings.blob_root.rglob("source"))
    services.blob_store.health = Mock(return_value=False)
    response = client.get("/api/v1/health")
    assert response.status_code == 503 and response.json()["code"] == "SERVICE_UNAVAILABLE"


@pytest.mark.parametrize("mode,code", [("hang", "AUDIT_TIMEOUT"), ("crash", "ENGINE_CRASH")])
def test_worker_controlled_failures(
    client: TestClient, services: SystemServices, mode: str, code: str
) -> None:
    _, ap = paths(upload(client).json())
    assert AuditWorker(replace(services, runner=TestRunner(mode))).run_once()
    result = client.get(ap, headers=auth()).json()
    assert result["status"] == "PROCESSING_FAILED"
    assert result["error"]["code"] == code
    assert result["findings"] == []
    assert list(services.settings.blob_root.rglob("source"))


def test_findings_sorted_and_review_required(client: TestClient, services: SystemServices) -> None:
    _, ap = paths(upload(client).json())
    AuditWorker(replace(services, runner=TestRunner("findings"))).run_once()
    result = client.get(ap, headers=auth()).json()
    assert result["status"] == "REVIEW_REQUIRED"
    assert [f["worksheet"] for f in result["findings"]] == ["a", "b"]


def test_testing_namespace_isolation(services: SystemServices) -> None:
    settings = services.settings.model_copy(update={"enable_test_interface": True})
    services = replace(services, settings=settings, session_service=Mock())
    with TestClient(create_app(services=services)) as client:
        assert upload(client, headers=auth() | {"X-Test-Session": "unknown"}).status_code == 400
        for sid, subjects in (("s1", ["A"]), ("s2", ["A", "B"])):
            services.repository.create_test_session(
                TestSession(
                    session_id=sid,
                    owner_subject="T",
                    allowed_subjects=subjects,
                    state="PREPARING",
                    created_at=datetime.now(UTC),
                    revision="unknown",
                    cleanup_error=None,
                )
            )
            services.repository.mark_session_ready(sid)
        h1 = auth() | {"X-Test-Session": "s1"}
        response = upload(client, headers=h1)
        assert response.status_code == 202
        vp, ap = paths(response.json())
        assert client.get(vp, headers=h1).status_code == 200
        for h in (auth(), auth() | {"X-Test-Session": "s2"}):
            assert client.get(vp, headers=h).status_code == 404
            assert client.get(ap, headers=h).status_code == 404
        assert client.get(vp, headers=auth("b") | {"X-Test-Session": "s1"}).status_code == 403
        measure = services.repository.summarize_measurements("s1")
        assert measure.covered is None and measure.total is None


def test_missing_integrations_are_unavailable(tmp_path: Path) -> None:
    settings = SystemSettings(
        _env_file=None,
        metadata_db_path=tmp_path / "db",
        blob_root=tmp_path / "blobs",
        engine_factory=None,
        runner_factory=None,
        session_service_factory=None,
    )
    app = create_app(settings)
    assert not tmp_path.joinpath("db").exists()  # factory has no writes
    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 503
        assert client.get("/test/report").status_code == 404
        assert len(client.get("/openapi.json").json()["paths"]) == 13


def test_persistence_failure_is_controlled(client: TestClient, services: SystemServices) -> None:
    _, ap = paths(upload(client).json())
    services.repository.complete_audit = Mock(side_effect=StorageError("disk fault"))
    assert AuditWorker(services).run_once()
    result = client.get(ap, headers=auth()).json()
    assert result["status"] == "PROCESSING_FAILED"
    assert result["findings"] == []


def test_source_rechecked_after_runner(client: TestClient, services: SystemServices) -> None:
    _, ap = paths(upload(client).json())
    source = next(services.settings.blob_root.rglob("source"))
    services.blob_store.read_source = Mock(side_effect=[source.read_bytes(), b"tampered"])
    assert AuditWorker(services).run_once()
    result = client.get(ap, headers=auth()).json()
    assert result["status"] == "PROCESSING_FAILED"
    assert result["error"]["code"] == "SOURCE_INTEGRITY_FAILED"


def test_internal_faults_are_sanitized(client: TestClient, services: SystemServices) -> None:
    services.repository.get_version = Mock(side_effect=RuntimeError("secret-workbook-content"))
    response = client.get("/api/v1/workbooks/w/versions/v", headers=auth())
    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_ERROR"
    assert "secret-workbook-content" not in response.text
    assert response.headers["x-request-id"] == response.json()["requestId"]


def test_failed_validation_timeout_keeps_database_empty(services: SystemServices) -> None:
    settings = services.settings.model_copy(update={"validation_timeout_seconds": 0.3})
    services = replace(services, settings=settings, runner=TestRunner("validation_hang"))
    with TestClient(create_app(services=services)) as client:
        response = upload(client)
        assert response.status_code == 500
        assert response.json()["code"] == "VALIDATION_TIMEOUT"
    assert counts(services) == [0, 0, 0, 0]
    assert not list(services.settings.blob_root.rglob("source"))


def test_poll_expires_queue_when_worker_is_down(services: SystemServices) -> None:
    import time

    settings = services.settings.model_copy(update={"queue_timeout_seconds": 0.05})
    with TestClient(create_app(services=replace(services, settings=settings))) as client:
        _, ap = paths(upload(client).json())
        time.sleep(0.06)
        result = client.get(ap, headers=auth()).json()
        assert result["status"] == "PROCESSING_FAILED"
        assert result["error"]["code"] == "QUEUE_TIMEOUT"


def test_upload_unavailable_runner_is_503(services: SystemServices) -> None:
    with TestClient(create_app(services=replace(services, runner=None))) as client:
        response = upload(client)
        assert response.status_code == 503
        assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert counts(services) == [0, 0, 0, 0]


@pytest.mark.parametrize("suffix", ["", "/audits/a"])
def test_read_unavailable_metadata_and_invalid_ids(services: SystemServices, suffix: str) -> None:
    path = "/api/v1/workbooks/w/versions/v" + suffix
    with TestClient(create_app(services=replace(services, repository=None))) as client:
        response = client.get(path, headers=auth())
        assert response.status_code == 503
        assert set(response.json()) == {"code", "message", "requestId", "details"}
        response = client.get(path.replace("workbooks/w", "workbooks/bad.id"), headers=auth())
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_REQUEST"


def test_read_storage_faults_are_not_successful_results(
    client: TestClient, services: SystemServices
) -> None:
    vp, ap = paths(upload(client).json())
    services.blob_store.read_source = Mock(side_effect=StorageError("private-source-location"))
    response = client.get(vp, headers=auth())
    assert response.status_code == 500 and response.json()["code"] == "STORAGE_FAILED"
    assert "private-source-location" not in response.text
    services.repository.get_audit = Mock(side_effect=StorageError("private-database-location"))
    response = client.get(ap, headers=auth())
    assert response.status_code == 500 and response.json()["code"] == "STORAGE_FAILED"
    assert "private-database-location" not in response.text
