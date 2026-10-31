"""Focused orchestration evidence with labeled engine doubles, never six-rule evidence.

The correction child, deadlines, filesystem adapter and durable repository are
real. Workbook audit/editor behavior is an explicit test double until Dylan's
Phase 4 integration is available.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.audit.engine import OoxmlWorkbookEngine
from app.config import SystemSettings
from app.contracts.errors import (
    EngineExecutionError,
    LeaseConflictError,
    ProjectException,
    StorageError,
)
from app.contracts.models import (
    AuditEvaluation,
    AuditRecord,
    Finding,
    JobRecord,
    ProcessingLimits,
    ReviewItem,
    TestSession,
    ValidatedWorkbook,
    VersionRecord,
    WorkbookRecord,
)
from app.contracts.phase4 import RULE_IDS, Decision, RemediationJob, RemediationRecord
from app.contracts.ports import SystemServices
from app.jobs.remediation import execute_correction_bounded, validate_decisions_bounded
from app.jobs.worker import AuditWorker
from app.measurement.runner import MeasuredEngineRunner
from app.storage.blobs import FilesystemBlobStore, sha256
from app.storage.sqlite import SQLiteRepository


class CorrectionEngineDouble:
    """Synthetic findings/editor results for backend boundary tests only."""

    supported_rulesets = ("2.0.0",)

    def __init__(self, failure: str = "", editable: bool = False) -> None:
        self.failure = failure
        self.editable = editable

    def validate(self, data: bytes, filename: str, limits: ProcessingLimits) -> ValidatedWorkbook:
        if self.failure == "output_validation":
            raise RuntimeError("private-exception-must-not-leak")
        return OoxmlWorkbookEngine().validate(data, filename, limits)

    def audit(
        self, data: bytes, validated: ValidatedWorkbook, ruleset_version: str
    ) -> AuditEvaluation:
        if self.failure == "output_audit":
            raise RuntimeError("private-exception-must-not-leak")
        rule = "ALT_TEXT" if self.editable else "MERGED_CELLS"
        target = (
            "xl/drawings/drawing1.xml#id=1"
            if self.editable
            else "xl/worksheets/sheet1.xml#range=A1:B1"
        )
        finding = Finding(
            findingId=hashlib.sha256(
                json.dumps(
                    [
                        sha256(data),
                        ruleset_version,
                        rule,
                        "Expenses",
                        target,
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
            ruleId=rule,
            category="NON_TEXT_CONTENT" if self.editable else "MERGED_CELLS",
            worksheet="Expenses",
            target=target,
            classification="REQUIRES_USER_INPUT" if self.editable else "MANUAL_REVIEW",
            message="Synthetic worker test finding.",
            recommendation="Synthetic manual review recommendation.",
        )
        return AuditEvaluation(
            findings=[finding],
            limitations=["Synthetic audit double; does not evaluate six rules."],
            checks_performed=RULE_IDS,
            review_context=[
                ReviewItem(
                    findingId=finding.findingId,
                    allowedActions=["APPLY", "DECLINE", "MANUAL_REVIEW"]
                    if self.editable
                    else ["DECLINE", "MANUAL_REVIEW"],
                    currentValue={"title": None, "description": None} if self.editable else None,
                    reason="Synthetic manual-only backend fixture.",
                )
            ],
        )

    def validate_decisions(
        self,
        data: bytes,
        validated: ValidatedWorkbook,
        audit: AuditRecord,
        decisions: list[Decision],
    ) -> None:
        if self.failure == "preflight":
            raise ProjectException(422, "CONFLICTING_CORRECTIONS", "private details")

    def remediate(
        self,
        data: bytes,
        validated: ValidatedWorkbook,
        audit: AuditRecord,
        decisions: list[Decision],
    ) -> bytes:
        if self.failure == "editing":
            raise RuntimeError("private-exception-must-not-leak")
        if self.failure == "timeout":
            time.sleep(20)
        if self.failure == "environment":
            assert "PRIVATE_TEST_SECRET" not in os.environ
            assert "AWS_SECRET_ACCESS_KEY" not in os.environ
            assert Path("source").stat().st_mode & 0o777 == 0o400
        if self.failure == "oversize":
            return b"x" * 25_000_001
        return data

    def verify_preservation(
        self,
        data: bytes,
        candidate: bytes,
        validated: ValidatedWorkbook,
        audit: AuditRecord,
        decisions: list[Decision],
    ) -> None:
        if self.failure == "preservation":
            raise RuntimeError("private-exception-must-not-leak")
        if self.failure == "false_verifier":
            return False  # type: ignore[return-value]
        assert data == candidate

    def verify_corrections(
        self,
        data: bytes,
        candidate: bytes,
        audit: AuditRecord,
        decisions: list[Decision],
        evaluation: AuditEvaluation,
    ) -> None:
        if self.failure == "correction_verification":
            raise RuntimeError("private-exception-must-not-leak")


class AuditOnlyEngineDouble:
    """A labeled six-rule auditor double deliberately lacking every editor method."""

    supported_rulesets = ("2.0.0",)

    def validate(self, data: bytes, filename: str, limits: ProcessingLimits) -> ValidatedWorkbook:
        return CorrectionEngineDouble().validate(data, filename, limits)

    def audit(
        self, data: bytes, validated: ValidatedWorkbook, ruleset_version: str
    ) -> AuditEvaluation:
        return CorrectionEngineDouble().audit(data, validated, ruleset_version)


def engine_fixture() -> tuple[bytes, ValidatedWorkbook, AuditRecord, list[Decision]]:
    data = (Path(__file__).parents[1] / "fixtures/synthetic/clean.xlsx").read_bytes()
    engine = CorrectionEngineDouble()
    validated = engine.validate(data, "clean.xlsx", ProcessingLimits())
    evaluation = engine.audit(data, validated, "2.0.0")
    now = datetime.now(UTC)
    audit = AuditRecord(
        audit_id="audit",
        workbook_id="workbook",
        version_id="version",
        owner_subject="owner",
        session_id=None,
        ruleset_version="2.0.0",
        status="REVIEW_REQUIRED",
        started_at=now,
        completed_at=now,
        findings=evaluation.findings,
        limitations=evaluation.limitations,
        error=None,
        review_context=evaluation.review_context,
        checks_performed=RULE_IDS,
    )
    return (
        data,
        validated,
        audit,
        [Decision(findingId=evaluation.findings[0].findingId, action="DECLINE")],
    )


def test_correction_children_use_bounded_files_and_no_credential_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("PRIVATE_TEST_SECRET", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "must-not-cross")
    data, validated, audit, decisions = engine_fixture()
    result = execute_correction_bounded(
        CorrectionEngineDouble("environment"),
        "remediate",
        data,
        validated,
        audit,
        decisions,
        session_id="one",
        revision="unknown",
        timeout_seconds=5,
    )
    assert result.candidate == data
    assert result.measurement is not None
    assert result.measurement.session_id == "one"
    assert result.measurement.covered_lines == {}
    assert result.measurement.unavailable_reason == "Code measurement disabled."


def test_preflight_preserves_safe_error_code_without_exception_contents() -> None:
    with pytest.raises(ProjectException) as error:
        validate_decisions_bounded(CorrectionEngineDouble("preflight"), *engine_fixture())
    assert error.value.status_code == 422
    assert error.value.code == "CONFLICTING_CORRECTIONS"
    assert "private details" not in error.value.message


def test_false_preservation_outcome_cannot_publish_success() -> None:
    data, validated, audit, decisions = engine_fixture()
    with pytest.raises(EngineExecutionError) as error:
        execute_correction_bounded(
            CorrectionEngineDouble("false_verifier"),
            "verify_preservation",
            data,
            validated,
            audit,
            decisions,
            candidate=data,
        )
    assert error.value.code == "ENGINE_RESULT_INVALID"


def test_real_correction_deadline_kills_child_and_removes_private_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    started = time.monotonic()
    with pytest.raises(EngineExecutionError) as error:
        execute_correction_bounded(
            CorrectionEngineDouble("timeout"),
            "remediate",
            *engine_fixture(),
            timeout_seconds=0.3,
        )
    assert error.value.code == "PROCESSING_TIMEOUT"
    assert time.monotonic() - started < 3
    assert list(tmp_path.iterdir()) == []


def test_missing_editor_is_explicit_dependency_failure() -> None:
    with pytest.raises(ProjectException) as error:
        validate_decisions_bounded(OoxmlWorkbookEngine(), *engine_fixture())
    assert error.value.status_code == 503
    assert error.value.code == "SERVICE_UNAVAILABLE"


def seed_worker(
    tmp_path: Path,
    failure: str = "",
    *,
    session_id: str | None = None,
    fault: bool = False,
    editable: bool = False,
) -> tuple[SystemServices, VersionRecord, AuditRecord]:
    repository = SQLiteRepository(tmp_path / "metadata.sqlite3")
    repository.initialize()
    blobs = FilesystemBlobStore(tmp_path / "blobs")
    blobs.initialize()
    data, validated, audit, _ = engine_fixture()
    now = datetime.now(UTC)
    if session_id is not None:
        repository.create_test_session(
            TestSession(
                session_id=session_id,
                owner_subject="admin",
                allowed_subjects=["owner"],
                state="PREPARING",
                created_at=now,
                revision="unknown",
                cleanup_error=None,
                audit_failures_remaining=1 if fault else 0,
            )
        )
        repository.mark_session_ready(session_id)
    namespace = f"tests/{session_id}" if session_id else "ordinary"
    version = VersionRecord(
        version_id="version",
        workbook_id="workbook",
        owner_subject="owner",
        session_id=session_id,
        file_name="clean.xlsx",
        source_sha256=sha256(data),
        blob_key=f"{namespace}/workbook/version/source",
        size_bytes=len(data),
        worksheet_count=validated.worksheet_count,
        nonempty_cell_count=validated.nonempty_cell_count,
        created_at=now,
        status="PROCESSING",
        latest_audit_id="audit",
        source_version_id=None,
        validated=validated,
    )
    processing = AuditRecord.model_validate(
        audit.model_dump()
        | {
            "status": "PROCESSING",
            "started_at": now,
            "completed_at": None,
            "findings": [],
            "limitations": [],
            "review_context": None,
            "checks_performed": None,
            "session_id": session_id,
        }
    )
    blobs.put_source(version.blob_key, data)
    repository.create_upload(
        WorkbookRecord(
            workbook_id="workbook", owner_subject="owner", session_id=session_id, created_at=now
        ),
        version,
        processing,
        JobRecord(
            job_id="upload-job",
            audit_id="audit",
            workbook_id="workbook",
            version_id="version",
            session_id=session_id,
            state="QUEUED",
            created_at=now,
            queue_deadline=now + timedelta(seconds=15),
            worker_id=None,
            lease_expires_at=None,
        ),
    )
    engine = CorrectionEngineDouble(failure, editable=editable)
    services = SystemServices(
        settings=SystemSettings(_env_file=None, ruleset_version="2.0.0"),
        repository=repository,
        blob_store=blobs,
        engine=engine,
        runner=MeasuredEngineRunner(engine),
    )
    if not fault:
        repository.claim_next_job("seed", now, 60)
        repository.complete_audit(
            "upload-job",
            "seed",
            CorrectionEngineDouble(editable=editable).audit(data, validated, "2.0.0"),
            datetime.now(UTC),
        )
    return services, version, processing


def queue_remediation(services: SystemServices, *, apply: bool = False) -> RemediationJob:
    repository = services.repository
    assert isinstance(repository, SQLiteRepository)
    version = repository.get_version("workbook", "version")
    audit = repository.get_audit("workbook", "version", "audit")
    assert version is not None and audit is not None
    now = datetime.now(UTC)
    namespace = f"tests/{version.session_id}" if version.session_id else "ordinary"
    record = RemediationRecord(
        remediation_id="remediation",
        workbook_id="workbook",
        source_version_id="version",
        source_audit_id="audit",
        owner_subject="owner",
        session_id=version.session_id,
        ruleset_version="2.0.0",
        status="REMEDIATING",
        created_at=now,
        decisions=[
            Decision(
                findingId=audit.findings[0].findingId,
                action="APPLY",
                value={"title": "Approved title", "description": "Approved description"},
            )
            if apply
            else Decision(findingId=audit.findings[0].findingId, action="DECLINE")
        ],
        output_version_id=None,
        output_id=None,
        audit_id=None,
        error=None,
    )
    job = RemediationJob(
        job_id="remediation-job",
        remediation_id="remediation",
        workbook_id="workbook",
        version_id="version",
        session_id=version.session_id,
        state="QUEUED",
        created_at=now,
        queue_deadline=now + timedelta(seconds=15),
        worker_id=None,
        lease_expires_at=None,
        output_version_id="output-version",
        output_audit_id="output-audit",
        output_id="output",
        blob_key=f"{namespace}/workbook/output-version/source",
    )
    repository.create_remediation(version, record, job, "key", "digest", "/test-remediation")
    return job


def test_real_worker_commits_distinct_unchanged_output_and_immutable_report(tmp_path: Path) -> None:
    services, version, _ = seed_worker(tmp_path)
    repository = services.repository
    assert isinstance(repository, SQLiteRepository)
    old_report = repository.get_report("audit")
    old_audit = repository.get_audit("workbook", "version", "audit")
    queue_remediation(services)
    assert AuditWorker(services).run_once()
    output = repository.get_version("workbook", "output-version")
    remediation = repository.get_remediation("remediation")
    assert remediation is not None and remediation.status == "REMEDIATED"
    assert output is not None and output.status == "REMEDIATED"
    assert output.source_version_id == version.version_id
    assert output.source_sha256 == version.source_sha256
    assert output.version_id != version.version_id
    assert services.blob_store.read_source(output.blob_key) == services.blob_store.read_source(
        version.blob_key
    )
    assert repository.get_audit("workbook", "version", "audit") == old_audit
    assert repository.get_report("audit") == old_report
    import json

    report = json.loads(repository.get_report("output-audit"))
    assert report["sourceVersionId"] == "version"
    assert report["remediationId"] == "remediation"
    assert report["outputVersionId"] == "output-version"
    assert report["approvedChanges"] == []
    assert report["decisions"][0]["action"] == "DECLINE"
    assert report["decisions"][0]["value"] is None
    assert report["unresolvedFindings"] == report["findings"]
    restarted = SQLiteRepository(repository.path)
    restarted.initialize()
    assert restarted.get_report("output-audit") == repository.get_report("output-audit")
    assert restarted.get_remediation("remediation") == remediation


def test_no_selected_edit_needs_no_correction_engine(tmp_path: Path) -> None:
    services, source, _ = seed_worker(tmp_path)
    engine = AuditOnlyEngineDouble()
    services = SystemServices(
        settings=services.settings,
        repository=services.repository,
        blob_store=services.blob_store,
        engine=engine,
        runner=MeasuredEngineRunner(engine),
    )
    job = queue_remediation(services)
    assert AuditWorker(services).run_once()
    assert services.repository.get_remediation("remediation").status == "REMEDIATED"
    assert services.blob_store.read_source(job.blob_key) == services.blob_store.read_source(
        source.blob_key
    )


@pytest.mark.parametrize(
    "stage,code",
    [
        ("editing", "PROCESSING_FAILED"),
        ("preservation", "PRESERVATION_FAILED"),
        ("output_validation", "OUTPUT_VALIDATION_FAILED"),
        ("output_audit", "OUTPUT_AUDIT_FAILED"),
        ("correction_verification", "OUTPUT_VALIDATION_FAILED"),
        ("oversize", "ENGINE_RESULT_INVALID"),
    ],
)
def test_real_worker_failures_never_publish_partial_output(
    tmp_path: Path, stage: str, code: str
) -> None:
    services, version, _ = seed_worker(tmp_path, stage, editable=True)
    repository = services.repository
    assert isinstance(repository, SQLiteRepository)
    before = repository.get_audit("workbook", "version", "audit")
    report = repository.get_report("audit")
    queue_remediation(services, apply=True)
    assert AuditWorker(services).run_once()
    record = repository.get_remediation("remediation")
    assert record is not None and record.status == "PROCESSING_FAILED"
    assert record.error is not None and record.error.code == code
    assert record.output_id is None and record.output_version_id is None
    assert repository.get_version("workbook", "output-version") is None
    assert repository.get_audit("workbook", "output-version", "output-audit") is None
    assert repository.get_report("output-audit") is None
    assert repository.get_audit("workbook", "version", "audit") == before
    assert repository.get_report("audit") == report
    assert sha256(services.blob_store.read_source(version.blob_key)) == version.source_sha256


def test_storage_write_then_error_is_cleaned_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services, version, _ = seed_worker(tmp_path)
    repository, blobs = services.repository, services.blob_store
    assert isinstance(repository, SQLiteRepository)
    assert isinstance(blobs, FilesystemBlobStore)
    job = queue_remediation(services)
    original = blobs.put_source

    def write_then_fail(key: str, data: bytes) -> str:
        original(key, data)
        raise StorageError("simulated lost write response")

    monkeypatch.setattr(blobs, "put_source", write_then_fail)
    assert AuditWorker(services).run_once()
    assert repository.get_remediation("remediation").error.code == "STORAGE_FAILED"
    assert repository.pending_blob_cleanup() == [(job.blob_key, version.source_sha256)]
    assert blobs.read_source(job.blob_key)
    assert not AuditWorker(services).run_once()
    with pytest.raises(StorageError):
        blobs.read_source(job.blob_key)
    assert blobs.read_source(version.blob_key)


def test_publication_transaction_failure_rolls_back_metadata_and_bytes(tmp_path: Path) -> None:
    services, _, _ = seed_worker(tmp_path)
    repository, blobs = services.repository, services.blob_store
    assert isinstance(repository, SQLiteRepository)
    job = queue_remediation(services)
    with repository._db(write=True) as db:
        db.execute(
            "CREATE TRIGGER reject_output BEFORE INSERT ON versions "
            "WHEN NEW.id='output-version' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    assert AuditWorker(services).run_once()
    assert repository.get_remediation("remediation").error.code == "STORAGE_FAILED"
    assert repository.get_version("workbook", "output-version") is None
    assert repository.get_report("output-audit") is None
    with pytest.raises(StorageError):
        blobs.read_source(job.blob_key)


def test_lost_publication_acknowledgement_preserves_committed_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services, _, _ = seed_worker(tmp_path)
    repository, blobs = services.repository, services.blob_store
    assert isinstance(repository, SQLiteRepository)
    job = queue_remediation(services)
    original = repository.complete_remediation

    def commit_then_fail(*args: object) -> None:
        original(*args)
        raise StorageError("lost acknowledgement after commit")

    monkeypatch.setattr(repository, "complete_remediation", commit_then_fail)
    assert AuditWorker(services).run_once()
    assert repository.get_remediation("remediation").status == "REMEDIATED"
    assert blobs.read_source(job.blob_key)
    assert repository.pending_blob_cleanup() == []


def test_expired_lease_fences_publication_and_retains_cleanup_tombstone(tmp_path: Path) -> None:
    services, version, _ = seed_worker(tmp_path)
    repository, blobs = services.repository, services.blob_store
    assert isinstance(repository, SQLiteRepository)
    queued = queue_remediation(services)
    claim = repository.claim_next_remediation("lost-worker", datetime.now(UTC), 1)
    assert claim is not None
    repository.register_candidate(claim.job_id, "lost-worker", version.source_sha256)
    blobs.put_source(claim.blob_key, blobs.read_source(version.blob_key))
    restarted = SQLiteRepository(repository.path)
    assert restarted.recover_stale_jobs(datetime.now(UTC) + timedelta(seconds=2)) == 1
    assert restarted.get_remediation("remediation").error.code == "WORKER_LOST"
    with pytest.raises(LeaseConflictError):
        repository.register_candidate(queued.job_id, "lost-worker", version.source_sha256)
    assert not AuditWorker(services).run_once()
    with pytest.raises(StorageError):
        blobs.read_source(claim.blob_key)
    # Tombstone stays eligible if a crashed writer resumes after this sweep.
    blobs.put_source(claim.blob_key, blobs.read_source(version.blob_key))
    assert not AuditWorker(services).run_once()
    with pytest.raises(StorageError):
        blobs.read_source(claim.blob_key)


def test_durable_one_shot_fault_precedes_engine_and_preserves_source(tmp_path: Path) -> None:
    services, version, initial_audit = seed_worker(
        tmp_path, "output_audit", session_id="isolated", fault=True
    )
    repository = services.repository
    assert isinstance(repository, SQLiteRepository)
    assert repository.get_test_session("isolated").audit_failures_remaining == 0
    assert repository.get_job("upload-job").test_audit_failure
    assert AuditWorker(services).run_once()
    failed = repository.get_audit("workbook", "version", "audit")
    assert failed.status == "PROCESSING_FAILED"
    assert failed.error.code == "TEST_AUDIT_FAILURE"
    assert failed.error.message == "Audit failure requested by the isolated test session."
    assert failed.error.details == {"stage": "audit", "testOnly": True}
    assert failed.findings == [] and failed.checks_performed == []
    assert sha256(services.blob_store.read_source(version.blob_key)) == version.source_sha256
    now = datetime.now(UTC)
    repository.create_reaudit(
        repository.get_version("workbook", "version"),
        initial_audit.model_copy(update={"audit_id": "reaudit", "started_at": now}),
        JobRecord(
            job_id="reaudit-job",
            audit_id="reaudit",
            workbook_id="workbook",
            version_id="version",
            session_id="isolated",
            state="QUEUED",
            created_at=now,
            queue_deadline=now + timedelta(seconds=15),
            worker_id=None,
            lease_expires_at=None,
        ),
    )
    engine = CorrectionEngineDouble()
    resumed = SystemServices(
        settings=services.settings,
        repository=repository,
        blob_store=services.blob_store,
        engine=engine,
        runner=MeasuredEngineRunner(engine),
    )
    assert AuditWorker(resumed).run_once()
    assert repository.get_audit("workbook", "version", "reaudit").status == "REVIEW_REQUIRED"
    assert repository.get_audit("workbook", "version", "audit") == failed


def test_worker_total_deadline_fails_without_publishing(tmp_path: Path) -> None:
    services, version, _ = seed_worker(tmp_path, "timeout", editable=True)
    queue_remediation(services, apply=True)
    limited = SystemServices(
        settings=services.settings.model_copy(update={"remediation_timeout_seconds": 0.3}),
        repository=services.repository,
        blob_store=services.blob_store,
        runner=services.runner,
        engine=services.engine,
    )
    started = time.monotonic()
    assert AuditWorker(limited).run_once()
    assert time.monotonic() - started < 3
    record = services.repository.get_remediation("remediation")
    assert record.error.code == "PROCESSING_TIMEOUT"
    assert record.error.details == {"stage": "editing"}
    assert services.repository.get_version("workbook", "output-version") is None
    assert sha256(services.blob_store.read_source(version.blob_key)) == version.source_sha256


def test_source_integrity_error_is_not_hidden_by_isolated_fault(tmp_path: Path) -> None:
    services, version, _ = seed_worker(tmp_path, session_id="isolated", fault=True)
    repository, blobs = services.repository, services.blob_store
    assert isinstance(repository, SQLiteRepository)
    assert isinstance(blobs, FilesystemBlobStore)
    assert repository.get_job("upload-job").test_audit_failure
    assert repository.get_test_session("isolated").audit_failures_remaining == 0
    (blobs.root / version.blob_key).write_bytes(b"tampered")
    assert AuditWorker(services).run_once()
    audit = repository.get_audit("workbook", "version", "audit")
    assert audit.error.code == "SOURCE_INTEGRITY_FAILED"
    assert audit.error.details == {"stage": "source"}
    assert audit.findings == [] and audit.checks_performed == []
    assert audit.completed_at is not None
    assert repository.get_test_session("isolated").audit_failures_remaining == 0
    assert blobs.read_source(version.blob_key) == b"tampered"
    report = json.loads(repository.get_report("audit"))
    assert report["error"] == audit.error.model_dump(mode="json")
    assert report["checksPerformed"] == [] and report["findings"] == []


def test_session_cleanup_retains_intent_for_late_candidate_write(tmp_path: Path) -> None:
    services, version, _ = seed_worker(tmp_path, session_id="isolated")
    repository, blobs = services.repository, services.blob_store
    assert isinstance(repository, SQLiteRepository)
    assert isinstance(blobs, FilesystemBlobStore)
    source = blobs.read_source(version.blob_key)
    ordinary_key = "ordinary/ordinary-workbook/ordinary-version/source"
    blobs.put_source(ordinary_key, b"ordinary-data-must-survive")
    queued = queue_remediation(services)
    claim = repository.claim_next_remediation("lost-worker", datetime.now(UTC), 1)
    assert claim is not None
    repository.register_candidate(claim.job_id, "lost-worker", version.source_sha256)
    blobs.put_source(claim.blob_key, source)
    assert repository.recover_stale_jobs(datetime.now(UTC) + timedelta(seconds=2)) == 1
    keys = repository.begin_session_cleanup("isolated", "admin")
    assert set(keys) == {version.blob_key, queued.blob_key}
    for key in keys:
        blobs.delete_test_source(key, "isolated")
    repository.finish_session_cleanup("isolated")
    assert repository.get_version("workbook", "version") is None
    assert repository.get_remediation("remediation") is None
    assert repository.get_test_session("isolated").state == "CLEANED"
    assert (queued.blob_key, version.source_sha256) in repository.pending_blob_cleanup()
    with repository._db() as db:
        assert db.execute("SELECT count(*) FROM remediation_jobs").fetchone()[0] == 0
    restarted = SQLiteRepository(repository.path)
    restarted.initialize()
    worker = AuditWorker(
        SystemServices(
            settings=services.settings,
            repository=restarted,
            blob_store=blobs,
            engine=services.engine,
            runner=services.runner,
        )
    )
    # The original worker resumes external I/O after its metadata has been removed.
    # Repeated sweeps retain the intent across restart and multiple late writes.
    for _ in range(2):
        blobs.put_source(queued.blob_key, source)
        assert not worker.run_once()
        with pytest.raises(StorageError):
            blobs.read_source(queued.blob_key)
        assert (queued.blob_key, version.source_sha256) in restarted.pending_blob_cleanup()
        assert blobs.read_source(ordinary_key) == b"ordinary-data-must-survive"


def test_actual_worker_module_emits_correlated_safe_terminal_log(tmp_path: Path) -> None:
    import json

    services, _, _ = seed_worker(tmp_path)
    queue_remediation(services)
    repository, blobs = services.repository, services.blob_store
    assert isinstance(repository, SQLiteRepository)
    assert isinstance(blobs, FilesystemBlobStore)
    result = subprocess.run(
        [sys.executable, "-m", "app.jobs.worker", "--once"],
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "ENGINE_FACTORY": "tests.unit.test_phase4_worker:CorrectionEngineDouble",
            "METADATA_DB_PATH": str(repository.path),
            "BLOB_ROOT": str(blobs.root),
            "PRIVATE_TEST_SECRET": "never-log-this-secret",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    events = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    terminal = [event for event in events if event["event"] == "job_finished"]
    assert len(terminal) == 1
    event = terminal[0]
    assert event["code"] == "REMEDIATION_SUCCEEDED"
    assert event["jobId"] == "remediation-job"
    assert event["remediationId"] == "remediation"
    assert event["stage"] == "publication"
    assert isinstance(event["duration"], (int, float))
    assert "never-log-this-secret" not in result.stderr
    assert "Synthetic worker test finding" not in result.stderr
