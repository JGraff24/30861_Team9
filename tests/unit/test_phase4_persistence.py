"""Real SQLite migration, concurrency, immutable reports, and publication fencing."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event, local

import pytest

from app.auth.service import TokenAuthService
from app.config import SystemSettings
from app.contracts.errors import (
    IntegrityError,
    LeaseConflictError,
    ProjectException,
    SessionBusyError,
)
from app.contracts.models import (
    AuditEvaluation,
    AuditRecord,
    AuditStatus,
    Finding,
    JobRecord,
    JobState,
    Principal,
    ProjectError,
    ReviewItem,
    TestSession,
    ValidatedWorkbook,
    VersionRecord,
    VersionStatus,
    WorkbookRecord,
)
from app.contracts.phase4 import Decision, RemediationJob, RemediationRecord
from app.contracts.ports import SystemServices
from app.services.decisions import parse_remediation_request
from app.services.projects import ProjectService
from app.services.reports import canonical_request
from app.storage.sqlite import SCHEMA_V1, SQLiteRepository
from tests.unit.test_phase4_worker import CorrectionEngineDouble


@pytest.fixture
def repository(tmp_path: Path) -> SQLiteRepository:
    result = SQLiteRepository(tmp_path / "backend.sqlite3")
    result.initialize()
    return result


def records(suffix: str, workbook: WorkbookRecord | None = None, *, ruleset: str = "2.0.0"):
    now = datetime.now(UTC)
    if workbook is None:
        workbook = WorkbookRecord(
            workbook_id="workbook" + suffix, owner_subject="owner", session_id=None, created_at=now
        )
    validated = ValidatedWorkbook(
        source_sha256=hashlib.sha256(b"immutable source").hexdigest(),
        size_bytes=16,
        worksheet_count=1,
        nonempty_cell_count=1,
        worksheet_parts={"Sheet1": "xl/worksheets/sheet1.xml"},
    )
    version = VersionRecord(
        version_id="version" + suffix,
        workbook_id=workbook.workbook_id,
        owner_subject=workbook.owner_subject,
        session_id=workbook.session_id,
        file_name="same.xlsx",
        source_sha256=validated.source_sha256,
        blob_key=(f"tests/{workbook.session_id}" if workbook.session_id else "ordinary")
        + f"/{workbook.workbook_id}/version{suffix}/source",
        size_bytes=16,
        worksheet_count=1,
        nonempty_cell_count=1,
        created_at=workbook.created_at,
        status="PROCESSING",
        latest_audit_id="audit" + suffix,
        source_version_id=None,
        validated=validated,
    )
    audit = AuditRecord(
        audit_id="audit" + suffix,
        workbook_id=version.workbook_id,
        version_id=version.version_id,
        owner_subject=version.owner_subject,
        session_id=version.session_id,
        ruleset_version=ruleset,
        status="PROCESSING",
        started_at=now,
        completed_at=None,
        findings=[],
        limitations=[],
        error=None,
    )
    job = JobRecord(
        job_id="job" + suffix,
        audit_id=audit.audit_id,
        workbook_id=version.workbook_id,
        version_id=version.version_id,
        session_id=version.session_id,
        state="QUEUED",
        created_at=now,
        queue_deadline=now + timedelta(seconds=15),
        worker_id=None,
        lease_expires_at=None,
    )
    return workbook, version, audit, job


def evaluation() -> AuditEvaluation:
    finding = Finding(
        findingId="a" * 64,
        ruleId="WORKSHEET_NAME",
        category="WORKSHEET_NAME",
        worksheet="Sheet1",
        target="xl/worksheets/sheet1.xml#sheetId=1",
        classification="REQUIRES_USER_INPUT",
        message="A generic sheet name.",
        recommendation="Supply a descriptive name.",
    )
    return AuditEvaluation(
        findings=[finding],
        limitations=["Meaning needs author review."],
        review_context=[
            ReviewItem(
                findingId=finding.findingId,
                allowedActions=["APPLY", "DECLINE", "MANUAL_REVIEW"],
                currentValue={"name": "Sheet1"},
                reason="A rename is supported.",
            )
        ],
        checks_performed=[
            "ALT_TEXT",
            "WORKSHEET_NAME",
            "TABLE_HEADER",
            "MERGED_CELLS",
            "LINK_TEXT",
            "COLOR_CONTRAST",
        ],
    )


def seed(repository: SQLiteRepository, suffix: str = "", workbook: WorkbookRecord | None = None):
    workbook, version, audit, job = records(suffix, workbook)
    repository.create_upload(workbook, version, audit, job)
    claim = repository.claim_next_job("audit-worker", datetime.now(UTC), 60)
    assert claim is not None and claim.job_id == job.job_id
    repository.complete_audit(job.job_id, "audit-worker", evaluation(), datetime.now(UTC))
    return workbook, repository.get_version(workbook.workbook_id, version.version_id), audit, job


def remediation(version: VersionRecord, suffix: str = ""):
    now = datetime.now(UTC)
    record = RemediationRecord(
        remediation_id="remediation" + suffix,
        workbook_id=version.workbook_id,
        source_version_id=version.version_id,
        source_audit_id=version.latest_audit_id,
        owner_subject=version.owner_subject,
        session_id=version.session_id,
        status="REMEDIATING",
        created_at=now,
        decisions=[Decision(findingId="a" * 64, action="DECLINE")],
        output_version_id=None,
        output_id=None,
        audit_id=None,
        error=None,
    )
    job = RemediationJob(
        job_id="remediation-job" + suffix,
        remediation_id=record.remediation_id,
        workbook_id=version.workbook_id,
        version_id=version.version_id,
        session_id=version.session_id,
        state="QUEUED",
        created_at=now,
        queue_deadline=now + timedelta(seconds=15),
        worker_id=None,
        lease_expires_at=None,
        output_version_id="generated" + suffix,
        output_audit_id="generated-audit" + suffix,
        output_id="output" + suffix,
        blob_key=(f"tests/{version.session_id}" if version.session_id else "ordinary")
        + f"/{version.workbook_id}/generated{suffix}/source",
    )
    return record, job


def output(version: VersionRecord, job: RemediationJob):
    result = version.model_copy(
        update={
            "version_id": job.output_version_id,
            "source_version_id": version.version_id,
            "latest_audit_id": job.output_audit_id,
            "blob_key": job.blob_key,
            "file_name": "same-remediated.xlsx",
            "status": "PROCESSING",
        }
    )
    audit = AuditRecord(
        audit_id=job.output_audit_id,
        workbook_id=result.workbook_id,
        version_id=result.version_id,
        owner_subject=result.owner_subject,
        session_id=result.session_id,
        ruleset_version="2.0.0",
        status="PROCESSING",
        started_at=datetime.now(UTC),
        completed_at=None,
        findings=[],
        limitations=[],
        error=None,
    )
    return result, audit


def accept(repository: SQLiteRepository, version: VersionRecord, suffix: str = "", *, key="key"):
    record, job = remediation(version, suffix)
    path = f"/api/v1/workbooks/{version.workbook_id}/versions/{version.version_id}/remediations"
    response = repository.create_remediation(version, record, job, key, "d" * 64, path)
    return record, job, response, path


def test_empty_initialization_and_reopening(repository: SQLiteRepository) -> None:
    repository.initialize()
    reopened = SQLiteRepository(repository.path)
    reopened.initialize()
    assert reopened.health()
    with reopened._db() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT count(*) FROM record_order").fetchone()[0] == 0


@pytest.mark.parametrize("terminal", [False, True])
def test_phase3_migration_preserves_raw_payloads_and_does_not_invent_reports(
    tmp_path: Path,
    terminal: bool,
) -> None:
    path = tmp_path / "phase3.sqlite3"
    workbook, version, audit, job = records("legacy", ruleset="1.0.0")
    if terminal:
        audit = audit.model_copy(
            update={
                "status": AuditStatus.REVIEW_REQUIRED,
                "completed_at": datetime.now(UTC),
                "findings": evaluation().findings,
            }
        )
        version = version.model_copy(update={"status": VersionStatus.REVIEW_REQUIRED})
        job = job.model_copy(update={"state": JobState.DONE})
    # Legacy JSON lacks every Phase4-only field; preserve original serialized bytes too.
    raw = {
        "workbooks": workbook.model_dump_json(),
        "versions": version.model_dump_json(exclude={"validated"}),
        "audits": audit.model_dump_json(exclude={"review_context", "checks_performed"}),
        "jobs": job.model_dump_json(exclude={"test_audit_failure"}),
    }
    with sqlite3.connect(path) as db:
        for statement in SCHEMA_V1:
            db.execute(statement)
        db.execute("PRAGMA user_version=1")
        db.execute(
            "INSERT INTO workbooks VALUES (?,?,?,?)",
            (workbook.workbook_id, None, "owner", raw["workbooks"]),
        )
        db.execute(
            "INSERT INTO versions VALUES (?,?,?,?)",
            (version.version_id, workbook.workbook_id, version.blob_key, raw["versions"]),
        )
        db.execute(
            "INSERT INTO audits VALUES (?,?,?)", (audit.audit_id, version.version_id, raw["audits"])
        )
        db.execute(
            "INSERT INTO jobs VALUES (?,?,?,?)",
            (job.job_id, audit.audit_id, job.state, raw["jobs"]),
        )
    repository = SQLiteRepository(path)
    repository.initialize()
    repository.initialize()
    with repository._db() as db:
        for table, expected in raw.items():
            assert db.execute(f"SELECT payload FROM {table}").fetchone()[0] == expected
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    historical = repository.get_audit(workbook.workbook_id, version.version_id, audit.audit_id)
    assert historical.ruleset_version == "1.0.0"
    assert historical.review_context is None and historical.checks_performed is None
    assert repository.get_report(audit.audit_id) is None
    assert repository.has_active_operation(version.version_id) is not terminal


def test_history_ties_use_acceptance_order_and_reaudit_keeps_report(repository: SQLiteRepository):
    workbook, version, audit, _ = seed(repository, "z")
    source_report = repository.get_report(audit.audit_id)
    source_audit = repository.get_audit(workbook.workbook_id, version.version_id, audit.audit_id)
    _, second, _, _ = seed(repository, "a", workbook)
    _, _, new_audit, new_job = records("reaudit", workbook)
    new_audit = new_audit.model_copy(update={"version_id": version.version_id})
    new_job = new_job.model_copy(update={"version_id": version.version_id})
    repository.create_reaudit(version, new_audit, new_job)
    repository.claim_next_job("worker", datetime.now(UTC), 60)
    failure = ProjectError(
        code="TEST_AUDIT_FAILURE",
        message="Isolated failure.",
        requestId="request",
        details={"stage": "audit"},
    )
    repository.fail_audit(new_job.job_id, "worker", failure, datetime.now(UTC))
    versions, audits = repository.history_records(workbook.workbook_id)
    assert version.created_at == second.created_at
    assert [item.version_id for item in versions] == [version.version_id, second.version_id]
    assert [item.audit_id for item in audits] == [audit.audit_id, "audita", new_audit.audit_id]
    assert audits[-1].status == "PROCESSING_FAILED"
    assert (
        repository.get_audit(workbook.workbook_id, version.version_id, audit.audit_id)
        == source_audit
    )
    assert repository.get_report(audit.audit_id) == source_report
    failed_report = json.loads(repository.get_report(new_audit.audit_id))
    assert failed_report["checksPerformed"] == []
    assert failed_report["error"]["code"] == "TEST_AUDIT_FAILURE"


def test_same_key_concurrency_reserves_one_job_and_replays_after_completion(
    repository: SQLiteRepository,
):
    _, version, _, _ = seed(repository)
    barrier = Barrier(2)

    def submit(index: int):
        record, job = remediation(version, str(index))
        barrier.wait()
        return repository.create_remediation(version, record, job, "same", "d" * 64, "/same-path")

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, [0, 1]))
    assert responses[0] == responses[1]
    with repository._db() as db:
        assert db.execute("SELECT count(*) FROM remediations").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM remediation_jobs").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM outputs").fetchone()[0] == 1
    job = repository.claim_next_remediation("worker", datetime.now(UTC), 180)
    repository.register_candidate(job.job_id, "worker", version.source_sha256)
    generated, audit = output(version, job)
    repository.complete_remediation(
        job.job_id, "worker", generated, audit, evaluation(), datetime.now(UTC)
    )
    record, extra_job = remediation(version, "retry")
    assert (
        repository.create_remediation(version, record, extra_job, "same", "d" * 64, "/same-path")
        == responses[0]
    )
    assert repository.get_version(version.workbook_id, job.output_version_id).status == "REMEDIATED"
    assert repository.get_version(version.workbook_id, version.version_id) == version


def test_conflicting_key_and_competing_operation_are_distinct(repository: SQLiteRepository):
    _, version, _, _ = seed(repository)
    _, _, accepted, path = accept(repository, version)
    record, job = remediation(version, "competitor")
    with pytest.raises(ProjectException) as conflict:
        repository.create_remediation(version, record, job, "key", "e" * 64, path)
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(ProjectException) as conflict:
        repository.create_remediation(version, record, job, "different-key", "d" * 64, path)
    assert conflict.value.code == "ACTIVE_OPERATION"
    assert (
        repository.find_idempotency("owner", "ordinary", "POST", path, "key", "d" * 64) == accepted
    )
    assert repository.find_idempotency("foreign", "ordinary", "POST", path, "key", "d" * 64) is None


def test_concurrent_audit_and_remediation_have_one_acceptance(repository: SQLiteRepository):
    workbook, version, _, _ = seed(repository)
    barrier = Barrier(2)
    _, _, audit, job = records("competitor", workbook)
    audit = audit.model_copy(update={"version_id": version.version_id})
    job = job.model_copy(update={"version_id": version.version_id})

    def submit(kind: str):
        barrier.wait()
        try:
            if kind == "audit":
                repository.create_reaudit(version, audit, job)
            else:
                accept(repository, version)
            return "accepted"
        except ProjectException as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, ["audit", "remediation"])) == [
            "ACTIVE_OPERATION",
            "accepted",
        ]


def test_publication_rollback_and_failure_preserve_source_and_reports(repository: SQLiteRepository):
    _, version, source_audit, _ = seed(repository)
    source_report = repository.get_report(source_audit.audit_id)
    record, job, accepted, path = accept(repository, version)
    repository.claim_next_remediation("worker", datetime.now(UTC), 180)
    repository.register_candidate(job.job_id, "worker", version.source_sha256)
    generated, audit = output(version, job)
    with repository._db(write=True) as db:
        db.execute(
            "CREATE TRIGGER reject_publication BEFORE UPDATE ON remediation_jobs "
            "WHEN NEW.state='DONE' BEGIN "
            "SELECT RAISE(ABORT,'injected final write failure'); END"
        )
    with pytest.raises(IntegrityError):
        repository.complete_remediation(
            job.job_id, "worker", generated, audit, evaluation(), datetime.now(UTC)
        )
    assert repository.get_version(version.workbook_id, generated.version_id) is None
    assert repository.get_audit(version.workbook_id, generated.version_id, audit.audit_id) is None
    assert repository.get_report(audit.audit_id) is None
    assert repository.get_remediation(record.remediation_id).status == "REMEDIATING"
    assert repository.get_output(job.output_id).published is False
    assert repository.get_version(version.workbook_id, version.version_id) == version
    assert repository.get_report(source_audit.audit_id) == source_report
    repository.fail_remediation(
        job.job_id,
        "worker",
        ProjectError(
            code="STORAGE_FAILED",
            message="Publication failed.",
            requestId="request",
            details={"stage": "publication"},
        ),
        datetime.now(UTC),
    )
    assert repository.get_remediation(record.remediation_id).status == "PROCESSING_FAILED"
    assert repository.pending_blob_cleanup() == [(job.blob_key, version.source_sha256)]
    assert (
        repository.find_idempotency("owner", "ordinary", "POST", path, "key", "d" * 64) == accepted
    )


@pytest.mark.parametrize("claimed", [False, True])
def test_restart_recovery_fences_late_workers_and_retains_key(
    repository: SQLiteRepository, claimed: bool
):
    _, version, _, _ = seed(repository)
    record, job, accepted, path = accept(repository, version)
    if claimed:
        repository.claim_next_remediation("lost", datetime.now(UTC), 1)
        repository.register_candidate(job.job_id, "lost", version.source_sha256)
    restarted = SQLiteRepository(repository.path)
    assert restarted.recover_stale_jobs(datetime.now(UTC) + timedelta(seconds=20)) == 1
    assert restarted.recover_stale_jobs(datetime.now(UTC) + timedelta(seconds=20)) == 0
    result = restarted.get_remediation(record.remediation_id)
    assert result.status == "PROCESSING_FAILED"
    assert result.error.code == ("WORKER_LOST" if claimed else "PROCESSING_TIMEOUT")
    generated, audit = output(version, job)
    with pytest.raises(LeaseConflictError):
        restarted.complete_remediation(
            job.job_id, "lost", generated, audit, evaluation(), datetime.now(UTC)
        )
    assert (
        restarted.find_idempotency("owner", "ordinary", "POST", path, "key", "d" * 64) == accepted
    )
    assert restarted.get_version(version.workbook_id, generated.version_id) is None


@pytest.mark.parametrize("claimed", [False, True])
def test_audit_recovery_identifies_queue_vs_worker_failure_and_preserves_snapshot(
    repository: SQLiteRepository, claimed: bool, caplog
):
    workbook, version, audit, job = records("expiry")
    repository.create_upload(workbook, version, audit, job)
    if claimed:
        repository.claim_next_job("lost-worker", datetime.now(UTC), 1)
    now = datetime.now(UTC) + timedelta(seconds=20)
    with caplog.at_level("WARNING", logger="app.storage.sqlite"):
        assert repository.recover_stale_jobs(now) == 1
    failed = repository.get_audit(workbook.workbook_id, version.version_id, audit.audit_id)
    assert failed.error.details == {"stage": "worker" if claimed else "queue"}
    assert failed.error.code == ("WORKER_LOST" if claimed else "PROCESSING_TIMEOUT")
    event = next(record for record in caplog.records if record.message == "job_expiry_detected")
    assert event.jobId == job.job_id and event.auditId == audit.audit_id
    assert event.code == failed.error.code and event.stage == failed.error.details["stage"]
    report = repository.get_report(audit.audit_id)
    assert report is not None
    assert repository.recover_stale_jobs(now + timedelta(days=1)) == 0
    assert repository.get_audit(workbook.workbook_id, version.version_id, audit.audit_id) == failed
    assert repository.get_report(audit.audit_id) == report


def test_reports_and_terminal_audits_cannot_be_rewritten_by_database_update(
    repository: SQLiteRepository,
):
    _, version, audit, _ = seed(repository)
    with pytest.raises(IntegrityError), repository._db(write=True) as db:
        db.execute("UPDATE audits SET payload=payload WHERE id=?", (audit.audit_id,))
    with pytest.raises(IntegrityError), repository._db(write=True) as db:
        db.execute("UPDATE reports SET content=? WHERE audit_id=?", (b"replaced", audit.audit_id))
    assert repository.get_version(version.workbook_id, version.version_id) == version


def test_idempotency_canonicalization_preserves_text_and_ignores_order() -> None:
    first = parse_remediation_request(
        {
            "auditId": "audit",
            "decisions": [
                {
                    "findingId": "a" * 64,
                    "action": "APPLY",
                    "value": {"title": " T ", "description": "D"},
                },
                {"findingId": "b" * 64, "action": "DECLINE"},
            ],
        }
    )
    second = parse_remediation_request(
        {
            "decisions": [
                {"action": "DECLINE", "findingId": "b" * 64},
                {
                    "value": {"description": "D", "title": " T "},
                    "action": "APPLY",
                    "findingId": "a" * 64,
                },
            ],
            "auditId": "audit",
        }
    )
    changed = parse_remediation_request(
        {
            "auditId": "audit",
            "decisions": [
                {
                    "findingId": "a" * 64,
                    "action": "APPLY",
                    "value": {"title": "T", "description": "D"},
                },
                {"findingId": "b" * 64, "action": "DECLINE"},
            ],
        }
    )
    assert canonical_request(first) == canonical_request(second)
    assert canonical_request(first) != canonical_request(changed)


def test_canonical_digest_does_not_hide_explicit_invalid_null_values() -> None:
    omitted = parse_remediation_request(
        {"auditId": "audit", "decisions": [{"findingId": "a" * 64, "action": "DECLINE"}]}
    )
    explicit_null = parse_remediation_request(
        {
            "auditId": "audit",
            "decisions": [{"findingId": "a" * 64, "action": "DECLINE", "value": None}],
        }
    )
    assert canonical_request(omitted) != canonical_request(explicit_null)


@pytest.mark.parametrize("changed", [False, True])
def test_service_key_race_between_initial_lookup_and_state_check(
    repository: SQLiteRepository,
    monkeypatch: pytest.MonkeyPatch,
    changed: bool,
) -> None:
    """Both requests miss first lookup; one commits before the other's state check."""
    _, version, _, _ = seed(repository)
    settings = SystemSettings(ruleset_version="2.0.0")
    services = SystemServices(
        settings=settings,
        repository=repository,
        engine=CorrectionEngineDouble(),
        auth_service=TokenAuthService(settings),
    )
    projects = ProjectService(services)
    principal = Principal(subject="owner", role="user")
    barrier = Barrier(2)
    committed = Event()
    caller = local()
    original_lookup = repository.find_idempotency

    def scheduled_lookup(*args):
        result = original_lookup(*args)
        if not getattr(caller, "looked_up", False):
            caller.looked_up = True
            assert result is None
            barrier.wait(timeout=5)
            if caller.index == 1:
                assert committed.wait(timeout=5)
        return result

    monkeypatch.setattr(repository, "find_idempotency", scheduled_lookup)

    def submit(index: int):
        caller.index = index
        body = {
            "auditId": version.latest_audit_id,
            "decisions": [
                {
                    "findingId": "a" * 64,
                    "action": "MANUAL_REVIEW" if changed and index == 1 else "DECLINE",
                }
            ],
        }
        try:
            return projects.submit_remediation(
                version.workbook_id, version.version_id, principal, None, "same-key", body
            )
        except ProjectException as exc:
            return exc.code
        finally:
            if index == 0:
                committed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(submit, [0, 1]))
    assert not isinstance(first, str)
    if changed:
        assert second == "IDEMPOTENCY_CONFLICT"
    else:
        assert second == first
    with repository._db() as db:
        assert db.execute("SELECT count(*) FROM remediations").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM remediation_jobs").fetchone()[0] == 1


def test_generated_reaudit_retains_origin_decisions_and_report_bytes(repository: SQLiteRepository):
    workbook, version, source_audit, _ = seed(repository)
    _, job, _, _ = accept(repository, version)
    repository.claim_next_remediation("worker", datetime.now(UTC), 180)
    repository.register_candidate(job.job_id, "worker", version.source_sha256)
    generated, audit = output(version, job)
    repository.complete_remediation(
        job.job_id, "worker", generated, audit, evaluation(), datetime.now(UTC)
    )
    original = repository.get_report(audit.audit_id)
    _, _, reaudit, audit_job = records("output-reaudit", workbook)
    reaudit = reaudit.model_copy(update={"version_id": generated.version_id})
    audit_job = audit_job.model_copy(update={"version_id": generated.version_id})
    repository.create_reaudit(generated, reaudit, audit_job)
    assert repository.get_version(workbook.workbook_id, generated.version_id).status == "PROCESSING"
    repository.claim_next_job("audit-worker", datetime.now(UTC), 60)
    repository.complete_audit(audit_job.job_id, "audit-worker", evaluation(), datetime.now(UTC))
    report = json.loads(repository.get_report(reaudit.audit_id))
    assert report["sourceVersionId"] == version.version_id
    assert report["remediationId"] == job.remediation_id
    assert report["outputVersionId"] == generated.version_id
    assert report["outputId"] == job.output_id
    assert report["decisions"][0]["sourceAuditId"] == source_audit.audit_id
    assert report["decisions"][0]["action"] == "DECLINE"
    assert report["decisions"][0]["value"] is None
    assert report["approvedChanges"] == []
    assert repository.get_report(audit.audit_id) == original
    assert repository.get_version(workbook.workbook_id, generated.version_id).status == "REMEDIATED"


def test_unregistered_candidate_cannot_publish(repository: SQLiteRepository):
    _, version, _, _ = seed(repository)
    _, job, _, _ = accept(repository, version)
    repository.claim_next_remediation("worker", datetime.now(UTC), 180)
    generated, audit = output(version, job)
    with pytest.raises(IntegrityError):
        repository.complete_remediation(
            job.job_id, "worker", generated, audit, evaluation(), datetime.now(UTC)
        )
    assert repository.get_version(version.workbook_id, generated.version_id) is None
    assert repository.get_output(job.output_id).published is False


def test_output_reservation_cannot_reuse_a_committed_source_identity(repository: SQLiteRepository):
    _, version, _, _ = seed(repository)
    record, job = remediation(version)
    job = job.model_copy(
        update={"output_version_id": version.version_id, "blob_key": version.blob_key}
    )
    with pytest.raises(IntegrityError):
        repository.create_remediation(version, record, job, "key", "d" * 64, "/path")
    assert repository.get_remediation(record.remediation_id) is None
    assert not repository.has_active_operation(version.version_id)
    assert repository.pending_blob_cleanup() == []


def test_database_relationship_trigger_rejects_foreign_owner(repository: SQLiteRepository):
    workbook, version, _, _ = seed(repository)
    forged = version.model_copy(
        update={
            "version_id": "forged",
            "owner_subject": "foreign",
            "blob_key": "ordinary/workbook/forged/source",
        }
    )
    with pytest.raises(IntegrityError), repository._db(write=True) as db:
        db.execute(
            "INSERT INTO versions VALUES (?,?,?,?)",
            (forged.version_id, workbook.workbook_id, forged.blob_key, forged.model_dump_json()),
        )
    assert repository.get_version(workbook.workbook_id, "forged") is None


def test_session_cleanup_includes_outputs_reports_keys_and_preserves_ordinary(
    repository: SQLiteRepository,
):
    repository.create_test_session(
        TestSession(
            session_id="isolated",
            owner_subject="admin",
            allowed_subjects=["owner"],
            state="PREPARING",
            created_at=datetime.now(UTC),
            revision="unknown",
            cleanup_error=None,
        )
    )
    repository.mark_session_ready("isolated")
    workbook = WorkbookRecord(
        workbook_id="test-workbook",
        owner_subject="owner",
        session_id="isolated",
        created_at=datetime.now(UTC),
    )
    _, isolated, audit, _ = seed(repository, "isolated", workbook)
    record, job, _, path = accept(repository, isolated, "isolated")
    with pytest.raises(SessionBusyError):
        repository.begin_session_cleanup("isolated", "admin")
    repository.claim_next_remediation("worker", datetime.now(UTC), 180)
    repository.register_candidate(job.job_id, "worker", isolated.source_sha256)
    generated, generated_audit = output(isolated, job)
    repository.complete_remediation(
        job.job_id, "worker", generated, generated_audit, evaluation(), datetime.now(UTC)
    )
    ordinary_workbook, ordinary, ordinary_audit, _ = seed(repository, "ordinary")
    ordinary_report = repository.get_report(ordinary_audit.audit_id)
    with pytest.raises(IntegrityError):
        repository.begin_session_cleanup("isolated", "foreign-admin")
    keys = repository.begin_session_cleanup("isolated", "admin")
    assert set(keys) == {isolated.blob_key, generated.blob_key}
    repository.finish_session_cleanup("isolated")
    assert repository.get_remediation(record.remediation_id) is None
    assert repository.get_output(job.output_id) is None
    assert repository.get_report(audit.audit_id) is None
    assert repository.get_report(generated_audit.audit_id) is None
    assert repository.find_idempotency("owner", "isolated", "POST", path, "key", "d" * 64) is None
    assert repository.get_version(ordinary_workbook.workbook_id, ordinary.version_id) == ordinary
    assert repository.get_report(ordinary_audit.audit_id) == ordinary_report
