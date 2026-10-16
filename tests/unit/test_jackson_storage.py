"""Transactional queue/namespace invariants and immutable local/S3 behavior."""

import base64
import hashlib
import io
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from app.contracts.errors import (
    DuplicateIdentifierError,
    IntegrityError,
    LeaseConflictError,
    SessionBusyError,
    StorageError,
)
from app.contracts.models import (
    AuditEvaluation,
    AuditRecord,
    ExecutionMeasurement,
    JobRecord,
    ProjectError,
    TestSession,
    VersionRecord,
    WorkbookRecord,
)
from app.storage.blobs import FilesystemBlobStore, S3BlobStore
from app.storage.sqlite import SQLiteRepository


@pytest.fixture
def repository(tmp_path: Path) -> SQLiteRepository:
    repository = SQLiteRepository(tmp_path / "system.sqlite3")
    repository.initialize()
    return repository


def seed(repository: SQLiteRepository, sid: str | None = None, suffix: str = "") -> JobRecord:
    now = datetime.now(UTC)
    w, v, a, j = (prefix + suffix for prefix in ("w", "v", "a", "j"))
    key = f"tests/{sid}" if sid else "ordinary"
    repository.create_upload(
        WorkbookRecord(workbook_id=w, owner_subject="A", session_id=sid, created_at=now),
        VersionRecord(
            version_id=v,
            workbook_id=w,
            owner_subject="A",
            session_id=sid,
            file_name="name.xlsx",
            source_sha256="0" * 64,
            blob_key=f"{key}/{w}/{v}/source",
            size_bytes=1,
            worksheet_count=1,
            nonempty_cell_count=0,
            created_at=now,
            status="PROCESSING",
            latest_audit_id=a,
            source_version_id=None,
        ),
        AuditRecord(
            audit_id=a,
            workbook_id=w,
            version_id=v,
            owner_subject="A",
            session_id=sid,
            ruleset_version="1.0.0",
            status="PROCESSING",
            started_at=now,
            completed_at=None,
            findings=[],
            limitations=[],
            error=None,
        ),
        JobRecord(
            job_id=j,
            audit_id=a,
            workbook_id=w,
            version_id=v,
            session_id=sid,
            state="QUEUED",
            created_at=now,
            queue_deadline=now + timedelta(seconds=15),
            worker_id=None,
            lease_expires_at=None,
        ),
    )
    return repository.get_job(j)


def session(repository: SQLiteRepository, sid: str = "s", owner: str = "admin") -> None:
    repository.create_test_session(
        TestSession(
            session_id=sid,
            owner_subject=owner,
            allowed_subjects=["A"],
            state="PREPARING",
            created_at=datetime.now(UTC),
            revision="rev",
            cleanup_error=None,
        )
    )
    repository.mark_session_ready(sid)


def test_schema_initialize_is_idempotent_and_foreign_keys(repository: SQLiteRepository) -> None:
    repository.initialize()
    assert repository.health()
    with repository._db() as db:
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    with sqlite3.connect(repository.path) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(StorageError):
        repository.initialize()


def test_atomic_upload_rolls_back_all_records(repository: SQLiteRepository) -> None:
    seed(repository)
    # Force the last insert of another upload to fail; the first three must roll back.
    with repository._db(write=True) as db:
        db.execute(
            "CREATE TRIGGER reject_jobs BEFORE INSERT ON jobs BEGIN "
            "SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(IntegrityError):
        seed(repository, suffix="2")
    assert repository.get_version("w2", "v2") is None
    with repository._db() as db:
        assert db.execute("SELECT count(*) FROM workbooks").fetchone()[0] == 1


def test_concurrent_claims_and_lease_transitions(repository: SQLiteRepository) -> None:
    job = seed(repository)
    now = datetime.now(UTC)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(lambda worker: repository.claim_next_job(worker, now, 60), ("one", "two"))
        )
    assert sum(claim is not None for claim in claims) == 1
    claim = next(claim for claim in claims if claim is not None)
    result = AuditEvaluation(findings=[], limitations=[])
    with pytest.raises(LeaseConflictError):
        repository.complete_audit(job.job_id, "foreign", result, now)
    with pytest.raises(LeaseConflictError):
        repository.complete_audit(job.job_id, claim.worker_id, result, now + timedelta(seconds=61))
    repository.complete_audit(job.job_id, claim.worker_id, result, datetime.now(UTC))
    before = repository.get_audit("w", "v", "a")
    with pytest.raises(LeaseConflictError):
        repository.complete_audit(job.job_id, claim.worker_id, result, datetime.now(UTC))
    with pytest.raises(LeaseConflictError):
        repository.fail_audit(
            job.job_id,
            claim.worker_id,
            ProjectError(code="FAIL", message="failed", requestId="req", details={}),
            datetime.now(UTC),
        )
    assert repository.get_audit("w", "v", "a") == before


@pytest.mark.parametrize("running", [True, False])
def test_stale_restart_recovery(repository: SQLiteRepository, running: bool) -> None:
    job = seed(repository)
    if running:
        repository.claim_next_job("old-worker", datetime.now(UTC), 1)
    restarted = SQLiteRepository(repository.path)
    future = datetime.now(UTC) + timedelta(seconds=20)
    assert restarted.recover_stale_jobs(future) == 1
    assert restarted.recover_stale_jobs(future) == 0
    assert restarted.claim_next_job("new-worker", future, 60) is None
    assert restarted.get_job(job.job_id).state == "FAILED"
    audit = restarted.get_audit("w", "v", "a")
    assert audit.status == "PROCESSING_FAILED" and not audit.findings
    assert audit.error.code == ("WORKER_LEASE_EXPIRED" if running else "QUEUE_TIMEOUT")


def test_scoped_cleanup_tombstone_and_races(repository: SQLiteRepository) -> None:
    session(repository)
    session(repository, "other")
    test_job = seed(repository, "s", "test")
    seed(repository, suffix="ordinary")
    seed(repository, "other", "other")
    with pytest.raises(SessionBusyError):
        repository.begin_session_cleanup("s", "admin")
    claim = repository.claim_next_job("worker", datetime.now(UTC), 60)
    assert claim.job_id == test_job.job_id
    repository.complete_audit(
        claim.job_id, "worker", AuditEvaluation(findings=[], limitations=[]), datetime.now(UTC)
    )
    with pytest.raises(IntegrityError):
        repository.begin_session_cleanup("s", "foreign")
    assert repository.begin_session_cleanup("s", "admin") == ["tests/s/wtest/vtest/source"]
    with pytest.raises(SessionBusyError):
        seed(repository, "s", "racing")
    repository.mark_session_failed("s", "Blob cleanup failed; retry is required.")
    assert repository.get_test_session("s").state == "CLEANED"
    assert repository.begin_session_cleanup("s", "admin") == ["tests/s/wtest/vtest/source"]
    repository.finish_session_cleanup("s")
    assert repository.get_version("wtest", "vtest") is None
    assert repository.get_version("wordinary", "vordinary") is not None
    assert repository.get_version("wother", "vother") is not None
    assert repository.get_test_session("s").cleanup_error is None
    with pytest.raises(DuplicateIdentifierError):
        session(repository)
    with pytest.raises(SessionBusyError):
        repository.mark_session_ready("s")


def test_duplicate_sessions_are_transactional(repository: SQLiteRepository) -> None:
    def create(_: int) -> str:
        try:
            session(repository)
            return "created"
        except DuplicateIdentifierError:
            return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(create, (1, 2))) == ["created", "duplicate"]


def test_measurements_sets_unavailable_and_revision(repository: SQLiteRepository) -> None:
    session(repository)
    assert repository.summarize_measurements("s").covered is None
    base = ExecutionMeasurement(
        session_id="s",
        invocation_id="i1",
        revision="rev",
        executable_lines={"app/audit/rules.py": [1, 2, 3]},
        covered_lines={"app/audit/rules.py": [1]},
        unavailable_reason=None,
    )
    repository.record_measurement(base)
    repository.record_measurement(base)
    repository.record_measurement(
        base.model_copy(
            update={
                "invocation_id": "i2",
                "covered_lines": {"app/audit/rules.py": [1, 2]},
            }
        )
    )
    assert repository.summarize_measurements("s").model_dump() | {} == {
        "covered": 2,
        "total": 3,
        "basis": "Validation/audit child line sets only; not whole-system coverage.",
    }
    with pytest.raises(IntegrityError):
        repository.record_measurement(base.model_copy(update={"revision": "other"}))
    with pytest.raises(IntegrityError):
        repository.record_measurement(base.model_copy(update={"covered_lines": {}}))
    repository.record_measurement(
        base.model_copy(
            update={
                "invocation_id": "i3",
                "unavailable_reason": "Timeout interrupted instrumentation.",
            }
        )
    )
    summary = repository.summarize_measurements("s")
    assert summary.covered is None and summary.total is None


def test_local_immutable_keys_and_cleanup(tmp_path: Path) -> None:
    blobs = FilesystemBlobStore(tmp_path / "sources")
    blobs.initialize()
    key = "ordinary/w/v/source"
    digest = blobs.put_source(key, b"original")
    with pytest.raises(StorageError):
        blobs.put_source(key, b"replacement")
    assert blobs.read_source(key) == b"original"
    with pytest.raises(IntegrityError):
        blobs.put_source("../../escape", b"bad")
    with pytest.raises(IntegrityError):
        blobs.delete_test_source(key, "s")
    with pytest.raises(IntegrityError):
        blobs.delete_uncommitted_source(key, "0" * 64)
    assert blobs.read_source(key) == b"original"
    blobs.delete_uncommitted_source(key, digest)
    blobs.delete_uncommitted_source(key, digest)
    key = "tests/s/w/v/source"
    blobs.put_source(key, b"test")
    with pytest.raises(IntegrityError):
        blobs.delete_test_source(key, "foreign")
    blobs.delete_test_source(key, "s")
    blobs.delete_test_source(key, "s")


def test_s3_stubs_conditional_encrypted_checksum_no_network() -> None:
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="test-only",
        aws_secret_access_key="test-only",
    )
    store = S3BlobStore("test-bucket", "us-east-1", client=client)
    data = b"original"
    digest = hashlib.sha256(data).hexdigest()
    checksum = base64.b64encode(bytes.fromhex(digest)).decode()
    key = "ordinary/w/v/source"
    with Stubber(client) as stub:
        stub.add_response("head_bucket", {}, {"Bucket": "test-bucket"})
        assert store.health()
        parameters = {
            "Bucket": "test-bucket",
            "Key": "sources/" + key,
            "Body": data,
            "IfNoneMatch": "*",
            "ServerSideEncryption": "AES256",
            "ChecksumSHA256": checksum,
            "Metadata": {"sha256": digest},
            "ContentType": "application/octet-stream",
        }
        stub.add_response("put_object", {}, parameters)
        assert store.put_source(key, data) == digest
        stub.add_client_error(
            "put_object",
            service_error_code="PreconditionFailed",
            http_status_code=412,
            expected_params=parameters,
        )
        with pytest.raises(StorageError):
            store.put_source(key, data)
        stub.add_response(
            "get_object",
            {
                "Body": StreamingBody(io.BytesIO(data), len(data)),
                "Metadata": {"sha256": digest},
                "ChecksumSHA256": checksum,
            },
            {"Bucket": "test-bucket", "Key": "sources/" + key, "ChecksumMode": "ENABLED"},
        )
        assert store.read_source(key) == data
        stub.add_response(
            "get_object",
            {
                "Body": StreamingBody(io.BytesIO(b"bad"), 3),
                "Metadata": {"sha256": digest},
            },
            {"Bucket": "test-bucket", "Key": "sources/" + key, "ChecksumMode": "ENABLED"},
        )
        with pytest.raises(IntegrityError):
            store.read_source(key)
        with pytest.raises(IntegrityError):
            store.delete_test_source(key, "s")
        stub.add_response(
            "delete_object", {}, {"Bucket": "test-bucket", "Key": "sources/tests/s/w/v/source"}
        )
        store.delete_test_source("tests/s/w/v/source", "s")
        stub.assert_no_pending_responses()


def test_terminal_transaction_failure_does_not_publish_partial_audit(
    repository: SQLiteRepository,
) -> None:
    seed(repository)
    claim = repository.claim_next_job("worker", datetime.now(UTC), 60)
    with repository._db(write=True) as db:
        db.execute(
            "CREATE TRIGGER reject_completion BEFORE UPDATE ON jobs "
            "WHEN NEW.state='DONE' BEGIN SELECT RAISE(ABORT, 'disk fault'); END"
        )
    with pytest.raises(IntegrityError):
        repository.complete_audit(
            claim.job_id, "worker", AuditEvaluation(findings=[], limitations=[]), datetime.now(UTC)
        )
    assert repository.get_audit("w", "v", "a").status == "PROCESSING"
    assert repository.get_version("w", "v").status == "PROCESSING"
    assert repository.get_job(claim.job_id).state == "RUNNING"
    repository.fail_audit(
        claim.job_id,
        "worker",
        ProjectError(
            code="PERSISTENCE_FAILED", message="Processing failed.", requestId="request", details={}
        ),
        datetime.now(UTC),
    )
    assert repository.get_audit("w", "v", "a").status == "PROCESSING_FAILED"


def test_s3_default_client_requires_role_credentials_and_is_lazy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    sdk_session = Mock()
    sdk_session.get_credentials.return_value.method = "env"
    factory = Mock(return_value=sdk_session)
    monkeypatch.setattr(boto3, "Session", factory)
    store = S3BlobStore("test-bucket", "us-east-1")
    factory.assert_not_called()
    with pytest.raises(StorageError):
        store.put_source("ordinary/w/v/source", b"data")
    sdk_session.client.assert_not_called()
    sdk_session.get_credentials.return_value.method = "iam-role"
    sdk_session.client.return_value.put_object.return_value = {}
    assert store.put_source("ordinary/w/v/source", b"data") == hashlib.sha256(b"data").hexdigest()
    assert sdk_session.client.call_args.kwargs.keys() == {"config"}
