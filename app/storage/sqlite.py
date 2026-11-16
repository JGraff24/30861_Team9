"""SQLite metadata, durable queue, test namespaces and real line-set aggregation."""

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from app.contracts.errors import (
    DuplicateIdentifierError,
    IntegrityError,
    LeaseConflictError,
    ProjectException,
    RecordNotFoundError,
    SessionBusyError,
    StorageError,
)
from app.contracts.models import (
    AuditEvaluation,
    AuditRecord,
    AuditStatus,
    CoverageMeasure,
    ExecutionMeasurement,
    JobRecord,
    JobState,
    ProjectError,
    SessionState,
    TestSession,
    VersionRecord,
    VersionStatus,
    WorkbookRecord,
)
from app.contracts.phase4 import (
    OutputRecord,
    RemediationAccepted,
    RemediationJob,
    RemediationRecord,
)
from app.contracts.phase5 import (
    ClockResponse,
    Notification,
    NotificationResponse,
    ScheduleRequest,
    ScheduleResponse,
    add_days,
    reminder_time,
)
from app.services.reports import make_audit_result, report_snapshot
from app.test_interface.activity import DEPLOYMENT_PROTECTION_SECONDS

logger = logging.getLogger(__name__)

# Versioned schema, relational ownership and FK constraints plus validated DTO snapshots.
SCHEMA_V1 = (
    "CREATE TABLE sessions (id TEXT PRIMARY KEY, state TEXT NOT NULL, payload TEXT NOT NULL)",
    "CREATE TABLE workbooks (id TEXT PRIMARY KEY, session_id TEXT REFERENCES sessions(id), "
    "owner TEXT NOT NULL, payload TEXT NOT NULL)",
    "CREATE TABLE versions (id TEXT PRIMARY KEY, workbook_id TEXT NOT NULL REFERENCES "
    "workbooks(id), blob_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL)",
    "CREATE TABLE audits (id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES "
    "versions(id), payload TEXT NOT NULL)",
    "CREATE TABLE jobs (id TEXT PRIMARY KEY, audit_id TEXT NOT NULL UNIQUE REFERENCES "
    "audits(id), state TEXT NOT NULL CHECK(state IN ('QUEUED','RUNNING','DONE','FAILED')), "
    "payload TEXT NOT NULL)",
    "CREATE TABLE measurements (session_id TEXT NOT NULL REFERENCES sessions(id), "
    "invocation_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(session_id, in"
    "vocation_id))",
    "CREATE INDEX queue_state ON jobs(state)",
    "CREATE INDEX workbook_session ON workbooks(session_id)",
)


class SQLiteRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._initialization_failed = False

    @contextmanager
    def _db(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if self._initialization_failed:
            raise StorageError("Metadata initialization requires recovery")
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA secure_delete=ON")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise IntegrityError("Metadata constraint failed") from exc
        except sqlite3.Error as exc:
            raise StorageError("Metadata unavailable") from exc
        finally:
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()

    def initialize(self) -> None:
        self._initialization_failed = False
        try:
            self._initialize_schema()
        except BaseException:
            # Startup must fail closed even if migration reached schema 3 before
            # the independent deletion journal could be read/replayed.
            self._initialization_failed = True
            raise

    def _initialize_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
        with self._db(write=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 3:
                raise StorageError("Metadata schema is newer than this release")
            if version == 0:
                for statement in SCHEMA_V1:
                    db.execute(statement)
                db.execute("PRAGMA user_version=1")
            if version < 2:
                self._migrate_phase4(db)
                db.execute("PRAGMA user_version=2")
            if version < 3:
                self._migrate_phase5(db)
                db.execute("PRAGMA user_version=3")
        self._restore_deletion_fences()

    def health(self) -> bool:
        try:
            with self._db() as db:
                return db.execute("PRAGMA user_version").fetchone()[0] == 3 and (
                    db.execute("SELECT count(*) FROM jobs").fetchone() is not None
                )
        except StorageError:
            return False

    def create_upload(
        self, workbook: WorkbookRecord, version: VersionRecord, audit: AuditRecord, job: JobRecord
    ) -> None:
        if not (
            workbook.workbook_id == version.workbook_id == audit.workbook_id == job.workbook_id
            and version.version_id == audit.version_id == job.version_id
            and version.latest_audit_id == audit.audit_id == job.audit_id
            and workbook.owner_subject == version.owner_subject == audit.owner_subject
            and workbook.session_id == version.session_id == audit.session_id == job.session_id
            and version.status == VersionStatus.PROCESSING
            and version.source_version_id is None
            and audit.status == AuditStatus.PROCESSING
            and job.state == JobState.QUEUED
            and job.worker_id is None
            and job.lease_expires_at is None
            and job.queue_deadline > job.created_at
        ):
            raise IntegrityError("Upload records are inconsistent")
        namespace = f"tests/{workbook.session_id}" if workbook.session_id else "ordinary"
        if version.blob_key != f"{namespace}/{workbook.workbook_id}/{version.version_id}/source":
            raise IntegrityError("Source key does not match upload namespace")
        with self._db(write=True) as db:
            self._writable(db, workbook.workbook_id)
            if workbook.session_id is not None:
                session = self._session(db, workbook.session_id)
                if session.state != SessionState.READY:
                    raise SessionBusyError("Test session is not ready")
                if workbook.owner_subject not in session.allowed_subjects:
                    raise IntegrityError("Subject is not allowed in this test namespace")
            staging = db.execute(
                "SELECT deadline FROM upload_staging WHERE version_id=?", (version.version_id,)
            ).fetchone()
            if staging is not None:
                if datetime.fromisoformat(staging[0]) <= datetime.now(UTC):
                    raise StorageError("Upload storage reservation expired")
            existing = db.execute(
                "SELECT payload FROM workbooks WHERE id=?", (workbook.workbook_id,)
            ).fetchone()
            if existing is not None and WorkbookRecord.model_validate_json(existing[0]) != workbook:
                raise IntegrityError("Workbook metadata differs")
            if existing is None:
                db.execute(
                    "INSERT INTO workbooks VALUES (?,?,?,?)",
                    (
                        workbook.workbook_id,
                        workbook.session_id,
                        workbook.owner_subject,
                        workbook.model_dump_json(),
                    ),
                )
            effective = self._effective_now(db, version.session_id, version.created_at)
            version = version.model_copy(update={"created_at": effective})
            audit = audit.model_copy(update={"started_at": effective})
            if existing is None:
                workbook = workbook.model_copy(update={"created_at": effective})
                db.execute(
                    "UPDATE workbooks SET payload=? WHERE id=?",
                    (workbook.model_dump_json(), workbook.workbook_id),
                )
            job = self._consume_fault(db, job)
            self._reserve_operation(db, version.version_id, "audit", job.job_id)
            self._order(db, workbook.workbook_id, "version", version.version_id)
            self._order(db, workbook.workbook_id, "audit", audit.audit_id)
            db.execute(
                "INSERT INTO versions VALUES (?,?,?,?)",
                (
                    version.version_id,
                    version.workbook_id,
                    version.blob_key,
                    version.model_dump_json(),
                ),
            )
            db.execute(
                "INSERT INTO audits VALUES (?,?,?)",
                (
                    audit.audit_id,
                    audit.version_id,
                    audit.model_dump_json(),
                ),
            )
            db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?)",
                (
                    job.job_id,
                    job.audit_id,
                    job.state,
                    job.model_dump_json(),
                ),
            )

            db.execute("DELETE FROM upload_staging WHERE version_id=?", (version.version_id,))

    def get_version(self, workbook_id: str, version_id: str) -> VersionRecord | None:
        with self._db() as db:
            if self._deleting(db, workbook_id):
                return None
            row = db.execute(
                "SELECT payload FROM versions WHERE id=? AND workbook_id=?",
                (version_id, workbook_id),
            ).fetchone()
            return VersionRecord.model_validate_json(row[0]) if row else None

    def get_audit(self, workbook_id: str, version_id: str, audit_id: str) -> AuditRecord | None:
        with self._db() as db:
            if self._deleting(db, workbook_id):
                return None
            row = db.execute(
                "SELECT a.payload FROM audits a JOIN versions v ON a.version_id=v.id "
                "WHERE a.id=? AND v.id=? AND v.workbook_id=?",
                (audit_id, version_id, workbook_id),
            ).fetchone()
            return AuditRecord.model_validate_json(row[0]) if row else None

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._db() as db:
            row = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
            return JobRecord.model_validate_json(row[0]) if row else None

    @staticmethod
    def _save_job(db: sqlite3.Connection, job: JobRecord) -> None:
        db.execute(
            "UPDATE jobs SET state=?, payload=? WHERE id=?",
            (job.state, job.model_dump_json(), job.job_id),
        )

    def claim_next_job(self, worker_id: str, now: datetime, lease_seconds: int) -> JobRecord | None:
        if lease_seconds <= 0 or not worker_id:
            raise ValueError("A worker and positive lease are required")
        with self._db(write=True) as db:
            self._recover(db, now)
            rows = db.execute("SELECT payload FROM jobs WHERE state='QUEUED'").fetchall()
            jobs = sorted(
                (JobRecord.model_validate_json(row[0]) for row in rows),
                key=lambda job: (job.created_at, job.job_id),
            )
            if not jobs:
                return None
            claimed = jobs[0].model_copy(
                update={
                    "state": JobState.RUNNING,
                    "worker_id": worker_id,
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                }
            )
            self._save_job(db, claimed)
            return claimed

    def _terminal(
        self,
        db: sqlite3.Connection,
        job: JobRecord,
        evaluation: AuditEvaluation | None,
        error: ProjectError | None,
        completed_at: datetime,
    ) -> None:
        audit = AuditRecord.model_validate_json(
            db.execute(
                "SELECT payload FROM audits WHERE id=?",
                (job.audit_id,),
            ).fetchone()[0]
        )
        version = VersionRecord.model_validate_json(
            db.execute(
                "SELECT payload FROM versions WHERE id=?",
                (job.version_id,),
            ).fetchone()[0]
        )
        if audit.status != AuditStatus.PROCESSING or version.status != VersionStatus.PROCESSING:
            raise LeaseConflictError("Audit already terminal")
        effective_completion = max(
            audit.started_at, self._effective_now(db, job.session_id, completed_at)
        )
        updated = make_audit_result(audit, evaluation, error, effective_completion)
        status = updated.status.value
        if version.source_version_id is not None and error is None:
            status = "REMEDIATED"
        updated_version = VersionRecord.model_validate(version.model_dump() | {"status": status})
        if audit.ruleset_version == "2.0.0":
            remediation, source_audit = self._provenance(db, version.version_id)
            snapshot = report_snapshot(version, updated, remediation, source_audit)
            db.execute("INSERT INTO reports VALUES (?,?)", (audit.audit_id, snapshot))
        db.execute(
            "DELETE FROM active_operations WHERE version_id=? AND operation_id=?",
            (job.version_id, job.job_id),
        )
        db.execute(
            "UPDATE audits SET payload=? WHERE id=?", (updated.model_dump_json(), job.audit_id)
        )
        db.execute(
            "UPDATE versions SET payload=? WHERE id=?",
            (updated_version.model_dump_json(), job.version_id),
        )
        self._save_job(
            db,
            job.model_copy(
                update={
                    "state": JobState.FAILED if error else JobState.DONE,
                }
            ),
        )

    @staticmethod
    def _job(db: sqlite3.Connection, job_id: str) -> JobRecord:
        row = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise RecordNotFoundError("Job not found")
        return JobRecord.model_validate_json(row[0])

    def complete_audit(
        self, job_id: str, worker_id: str, evaluation: AuditEvaluation, completed_at: datetime
    ) -> None:
        with self._db(write=True) as db:
            job = self._job(db, job_id)
            if (
                job.state != JobState.RUNNING
                or job.worker_id != worker_id
                or job.lease_expires_at is None
                or completed_at >= job.lease_expires_at
            ):
                raise LeaseConflictError("Worker has no live lease")
            self._terminal(db, job, evaluation, None, completed_at)

    def fail_audit(
        self, job_id: str, worker_id: str | None, error: ProjectError, completed_at: datetime
    ) -> None:
        with self._db(write=True) as db:
            job = self._job(db, job_id)
            if job.state in (JobState.DONE, JobState.FAILED):
                raise LeaseConflictError("Job already terminal")
            if job.state == JobState.RUNNING:
                if (
                    worker_id != job.worker_id
                    or job.lease_expires_at is None
                    or completed_at >= job.lease_expires_at
                ):
                    raise LeaseConflictError("Worker has no live lease")
            elif worker_id is not None:
                raise LeaseConflictError("Unclaimed job")
            self._terminal(db, job, None, error, completed_at)

    def _recover(self, db: sqlite3.Connection, now: datetime) -> int:
        count = 0
        for row in db.execute(
            "SELECT payload FROM jobs WHERE state IN ('QUEUED','RUNNING')"
        ).fetchall():
            job = JobRecord.model_validate_json(row[0])
            expired = (job.state == JobState.QUEUED and job.queue_deadline <= now) or (
                job.state == JobState.RUNNING
                and (job.lease_expires_at is None or job.lease_expires_at <= now)
            )
            if expired:
                audit = self.get_audit_for_job(db, job)
                stage = "queue" if job.state == JobState.QUEUED else "worker"
                code = (
                    ("PROCESSING_TIMEOUT" if job.state == JobState.QUEUED else "WORKER_LOST")
                    if audit.ruleset_version == "2.0.0"
                    else (
                        "QUEUE_TIMEOUT" if job.state == JobState.QUEUED else "WORKER_LEASE_EXPIRED"
                    )
                )
                self._terminal(
                    db,
                    job,
                    None,
                    ProjectError(
                        code=code,
                        message="Processing deadline expired.",
                        requestId=job.job_id,
                        details={"stage": stage} if audit.ruleset_version == "2.0.0" else {},
                    ),
                    now,
                )
                logger.warning(
                    "job_expiry_detected",
                    extra={
                        "jobId": job.job_id,
                        "auditId": job.audit_id,
                        "sessionId": job.session_id,
                        "stage": stage,
                        "code": code,
                    },
                )
                count += 1
        count += self._recover_remediations(db, now)
        return count

    def recover_stale_jobs(self, now: datetime) -> int:
        with self._db(write=True) as db:
            return self._recover(db, now)

    @staticmethod
    def _session(db: sqlite3.Connection, session_id: str) -> TestSession:
        row = db.execute("SELECT payload FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            raise RecordNotFoundError("Test session not found")
        return TestSession.model_validate_json(row[0])

    @staticmethod
    def _save_session(db: sqlite3.Connection, session: TestSession) -> None:
        db.execute(
            "UPDATE sessions SET state=?,payload=? WHERE id=?",
            (session.state, session.model_dump_json(), session.session_id),
        )

    def create_test_session(self, session: TestSession) -> None:
        with self._db(write=True) as db:
            if db.execute("SELECT 1 FROM sessions WHERE id=?", (session.session_id,)).fetchone():
                raise DuplicateIdentifierError("Test session ID is reserved")
            if session.state != SessionState.PREPARING:
                raise IntegrityError("New session must be preparing")
            db.execute(
                "INSERT INTO sessions VALUES (?,?,?)",
                (session.session_id, session.state, session.model_dump_json()),
            )

    def get_test_session(self, session_id: str) -> TestSession | None:
        with self._db() as db:
            try:
                return self._session(db, session_id)
            except RecordNotFoundError:
                return None

    def mark_session_ready(self, session_id: str) -> None:
        with self._db(write=True) as db:
            session = self._session(db, session_id)
            if session.state != SessionState.PREPARING:
                raise SessionBusyError("Only preparing sessions can become ready")
            self._save_session(db, session.model_copy(update={"state": SessionState.READY}))

    def renew_test_session_protection(
        self, session_id: str, subject: str, protected_until: datetime
    ) -> bool:
        with self._db(write=True) as db:
            try:
                session = self._session(db, session_id)
            except RecordNotFoundError:
                return False
            if session.state != SessionState.READY or subject not in session.allowed_subjects:
                return False
            if (
                session.deployment_protected_until is None
                or session.deployment_protected_until < protected_until
            ):
                self._save_session(
                    db, session.model_copy(update={"deployment_protected_until": protected_until})
                )
            return True

    def mark_session_failed(self, session_id: str, reason: str) -> None:
        with self._db(write=True) as db:
            session = self._session(db, session_id)
            # CLEANED remains a tombstone even if blob deletion failed.
            state = (
                SessionState.CLEANED
                if session.state == SessionState.CLEANED
                else (SessionState.FAILED)
            )
            self._save_session(
                db,
                session.model_copy(
                    update={
                        "state": state,
                        "cleanup_error": reason,
                    }
                ),
            )

    def begin_session_cleanup(self, session_id: str, owner_subject: str) -> list[str]:
        with self._db(write=True) as db:
            session = self._session(db, session_id)
            if session.owner_subject != owner_subject:
                raise IntegrityError("Test namespace belongs to another administrator")
            rows = db.execute(
                "SELECT j.state FROM jobs j JOIN audits a ON j.audit_id=a.id "
                "JOIN versions v ON a.version_id=v.id JOIN workbooks w ON v.workbook_id=w.id "
                "WHERE w.session_id=? AND j.state IN ('QUEUED','RUNNING')",
                (session_id,),
            ).fetchall()
            remediation_active = db.execute(
                (
                    "SELECT 1 FROM remediation_jobs j JOIN remediations r ON j.remediation_id=r.id "
                    "JOIN versions v ON r.source_version_id=v.id JOIN workbooks w ON v.workbook_id="
                    "w.id WHERE w.session_id=? AND j.state IN ('QUEUED','RUNNING')"
                ),
                (session_id,),
            ).fetchone()
            staging_active = any(
                datetime.fromisoformat(row[0]) > datetime.now(UTC)
                for row in db.execute(
                    "SELECT deadline FROM upload_staging WHERE session_id=?", (session_id,)
                )
            )
            if rows or remediation_active or staging_active:
                raise SessionBusyError("Namespace still has active jobs")
            self._save_session(
                db,
                session.model_copy(
                    update={
                        "state": SessionState.CLEANED,
                        "cleanup_error": "Cleanup reserved; blob deletion pending.",
                    }
                ),
            )
            return (
                [
                    row[0]
                    for row in db.execute(
                        "SELECT v.blob_key FROM versions v JOIN workbooks w ON v.workbook_id=w.id "
                        "WHERE w.session_id=?",
                        (session_id,),
                    )
                ]
                + [
                    row[0]
                    for row in db.execute(
                        (
                            "SELECT j.blob_key FROM remediation_jobs j JOIN remediations r ON j.rem"
                            "ediation"
                            "_id=r.id JOIN versions v ON r.source_version_id=v.id JOIN workbooks w "
                            "ON v.wor"
                            "kbook_id=w.id WHERE w.session_id=?"
                        ),
                        (session_id,),
                    )
                ]
                + [
                    row[0]
                    for row in db.execute(
                        "SELECT blob_key FROM upload_staging WHERE session_id=?", (session_id,)
                    )
                ]
            )

    def finish_session_cleanup(self, session_id: str) -> None:
        with self._db(write=True) as db:
            session = self._session(db, session_id)
            if session.state != SessionState.CLEANED:
                raise SessionBusyError("Cleanup has not been reserved")
            # Retain deletion intents beyond session resource cleanup. A fenced
            # worker may still finish external I/O after its metadata lease expired.
            db.execute(
                "INSERT OR IGNORE INTO retired_blobs SELECT j.blob_key,j.candidate_sha256 FROM "
                "remediation_jobs j JOIN remediations r ON j.remediation_id=r.id JOIN versions "
                "v ON r.source_version_id=v.id JOIN workbooks w ON v.workbook_id=w.id WHERE w.s"
                "ession_id=? AND j.candidate_sha256 IS NOT NULL",
                (session_id,),
            )
            db.execute(
                "INSERT OR IGNORE INTO retired_blobs SELECT blob_key,digest FROM upload_staging"
                " WHERE session_id=?",
                (session_id,),
            )
            db.execute("DELETE FROM upload_staging WHERE session_id=?", (session_id,))
            for table in ("schedules", "reminder_occurrences", "notifications", "deletions"):
                db.execute(f"DELETE FROM {table} WHERE session_id=?", (session_id,))
            session = session.model_copy(
                update={"clock_current_time": None, "faults_remaining": {}}
            )
            workbook_ids = "SELECT id FROM workbooks WHERE session_id=?"
            db.execute(
                f"DELETE FROM idempotency WHERE workbook_id IN ({workbook_ids})", (session_id,)
            )
            db.execute(f"DELETE FROM outputs WHERE workbook_id IN ({workbook_ids})", (session_id,))
            db.execute(
                "DELETE FROM remediation_jobs WHERE remediation_id IN (SELECT r.id "
                "FROM remediations r JOIN versions v ON r.source_version_id=v.id "
                f"WHERE v.workbook_id IN ({workbook_ids}))",
                (session_id,),
            )
            db.execute(
                "DELETE FROM remediations WHERE source_version_id IN "
                f"(SELECT id FROM versions WHERE workbook_id IN ({workbook_ids}))",
                (session_id,),
            )
            db.execute(
                "DELETE FROM reports WHERE audit_id IN (SELECT a.id FROM audits a "
                f"JOIN versions v ON a.version_id=v.id WHERE v.workbook_id IN ({workbook_ids}))",
                (session_id,),
            )
            db.execute(
                f"DELETE FROM record_order WHERE workbook_id IN ({workbook_ids})", (session_id,)
            )
            db.execute(
                "DELETE FROM active_operations WHERE version_id IN "
                f"(SELECT id FROM versions WHERE workbook_id IN ({workbook_ids}))",
                (session_id,),
            )
            db.execute(
                "DELETE FROM jobs WHERE audit_id IN (SELECT a.id FROM audits a "
                "JOIN versions v ON a.version_id=v.id JOIN workbooks w "
                "ON v.workbook_id=w.id WHERE w.session_id=?)",
                (session_id,),
            )
            db.execute(
                "DELETE FROM audits WHERE version_id IN (SELECT v.id FROM versions v "
                "JOIN workbooks w ON v.workbook_id=w.id WHERE w.session_id=?)",
                (session_id,),
            )
            db.execute(
                "DELETE FROM versions WHERE workbook_id IN "
                "(SELECT id FROM workbooks WHERE session_id=?)",
                (session_id,),
            )
            db.execute("DELETE FROM workbooks WHERE session_id=?", (session_id,))
            db.execute("DELETE FROM measurements WHERE session_id=?", (session_id,))
            self._save_session(db, session.model_copy(update={"cleanup_error": None}))

    def record_measurement(self, measurement: ExecutionMeasurement) -> None:
        with self._db(write=True) as db:
            session = self._session(db, measurement.session_id)
            if session.state != SessionState.READY or session.revision != measurement.revision:
                raise IntegrityError("Measurement scope/revision differs")
            row = db.execute(
                "SELECT payload FROM measurements WHERE session_id=? AND invocation_id=?",
                (measurement.session_id, measurement.invocation_id),
            ).fetchone()
            if row:
                if ExecutionMeasurement.model_validate_json(row[0]) != measurement:
                    raise IntegrityError("Measurement invocation is immutable")
                return
            db.execute(
                "INSERT INTO measurements VALUES (?,?,?)",
                (
                    measurement.session_id,
                    measurement.invocation_id,
                    measurement.model_dump_json(),
                ),
            )

    def summarize_measurements(self, session_id: str) -> CoverageMeasure:
        with self._db() as db:
            session = self._session(db, session_id)
            if session.state == SessionState.CLEANED:
                raise RecordNotFoundError("Namespace was cleaned")
            measurements = [
                ExecutionMeasurement.model_validate_json(row[0])
                for row in db.execute(
                    "SELECT payload FROM measurements WHERE session_id=?",
                    (session_id,),
                )
            ]
        basis = (
            "Child line sets in app/workbooks and app/audit from validation, audit, "
            "and any correction calls; excludes API, storage, UI, and modules outside those paths."
        )
        if not measurements or any(m.unavailable_reason for m in measurements):
            return CoverageMeasure(
                covered=None, total=None, basis=basis + " Instrumentation missing or unavailable."
            )
        executable: set[tuple[str, int]] = set()
        covered: set[tuple[str, int]] = set()
        for measurement in measurements:
            for field, aggregate in (
                (measurement.executable_lines, executable),
                (measurement.covered_lines, covered),
            ):
                aggregate.update((path, line) for path, lines in field.items() for line in lines)
        return CoverageMeasure(covered=len(covered), total=len(executable), basis=basis)

    @staticmethod
    def _migrate_phase4(db: sqlite3.Connection) -> None:
        """Additive migration; legacy payloads/reports are never relabeled or invented."""
        statements = (
            "CREATE TABLE retired_blobs (blob_key TEXT PRIMARY KEY, digest TEXT NOT NULL)",
            "CREATE TABLE upload_staging (version_id TEXT PRIMARY KEY, blob_key TEXT NOT NULL "
            "UNIQUE, digest TEXT NOT NULL, session_id TEXT REFERENCES sessions(id), "
            "deadline TEXT NOT NULL)",
            (
                "CREATE TABLE active_operations (version_id TEXT PRIMARY KEY REFERENCES version"
                "s(id) DEFERRABLE INITIALLY DEFERRED, kind TEXT NOT NULL CHECK(kind IN ('audit'"
                ",'remediation')), operation_id TEXT NOT NULL UNIQUE)"
            ),
            (
                "CREATE TABLE record_order (sequence INTEGER PRIMARY KEY AUTOINCREMENT, workboo"
                "k_id TEXT NOT NULL REFERENCES workbooks(id), kind TEXT NOT NULL CHECK(kind IN "
                "('version','audit')), record_id TEXT NOT NULL, UNIQUE(kind,record_id))"
            ),
            (
                "CREATE TABLE reports (audit_id TEXT PRIMARY KEY REFERENCES audits(id), content"
                " BLOB NOT NULL)"
            ),
            (
                "CREATE TABLE remediations (id TEXT PRIMARY KEY, source_version_id TEXT NOT NUL"
                "L REFERENCES versions(id), source_audit_id TEXT NOT NULL REFERENCES audits(id)"
                ", payload TEXT NOT NULL)"
            ),
            (
                "CREATE TABLE remediation_jobs (id TEXT PRIMARY KEY, remediation_id TEXT NOT NU"
                "LL UNIQUE REFERENCES remediations(id), state TEXT NOT NULL CHECK(state IN ('QU"
                "EUED','RUNNING','DONE','FAILED')), blob_key TEXT NOT NULL UNIQUE, candidate_sh"
                "a256 TEXT, payload TEXT NOT NULL)"
            ),
            (
                "CREATE TABLE outputs (id TEXT PRIMARY KEY, workbook_id TEXT NOT NULL REFERENCE"
                "S workbooks(id), version_id TEXT REFERENCES versions(id), remediation_id TEXT "
                "NOT NULL UNIQUE REFERENCES remediations(id), payload TEXT NOT NULL)"
            ),
            (
                "CREATE TABLE idempotency (owner TEXT NOT NULL, namespace TEXT NOT NULL, method"
                " TEXT NOT NULL, path TEXT NOT NULL, key TEXT NOT NULL, workbook_id TEXT NOT NU"
                "LL REFERENCES workbooks(id), digest TEXT NOT NULL, response TEXT NOT NULL, PRI"
                "MARY KEY(owner,namespace,method,path,key))"
            ),
            (
                "CREATE TRIGGER immutable_report BEFORE UPDATE ON reports BEGIN SELECT RAISE(AB"
                "ORT,'immutable report'); END"
            ),
            (
                "CREATE TRIGGER immutable_terminal_audit BEFORE UPDATE ON audits WHEN json_extr"
                "act(OLD.payload,'$.status') != 'PROCESSING' BEGIN SELECT RAISE(ABORT,'immutabl"
                "e audit'); END"
            ),
            (
                "CREATE TRIGGER version_relationship BEFORE INSERT ON versions WHEN NOT EXISTS "
                "(SELECT 1 FROM workbooks w WHERE w.id=NEW.workbook_id AND w.owner=json_extract"
                "(NEW.payload,'$.owner_subject') AND w.session_id IS json_extract(NEW.payload,'"
                "$.session_id')) OR (json_extract(NEW.payload,'$.source_version_id') IS NOT NUL"
                "L AND NOT EXISTS (SELECT 1 FROM versions v WHERE v.id=json_extract(NEW.payload"
                ",'$.source_version_id') AND v.workbook_id=NEW.workbook_id)) BEGIN SELECT RAISE"
                "(ABORT,'version relationship'); END"
            ),
            (
                "CREATE TRIGGER audit_relationship BEFORE INSERT ON audits WHEN NOT EXISTS (SEL"
                "ECT 1 FROM versions v JOIN workbooks w ON v.workbook_id=w.id WHERE v.id=NEW.ve"
                "rsion_id AND w.id=json_extract(NEW.payload,'$.workbook_id') AND w.owner=json_e"
                "xtract(NEW.payload,'$.owner_subject') AND w.session_id IS json_extract(NEW.pay"
                "load,'$.session_id')) BEGIN SELECT RAISE(ABORT,'audit relationship'); END"
            ),
            (
                "CREATE TRIGGER remediation_relationship BEFORE INSERT ON remediations WHEN NOT"
                " EXISTS (SELECT 1 FROM audits a JOIN versions v ON a.version_id=v.id JOIN work"
                "books w ON v.workbook_id=w.id WHERE a.id=NEW.source_audit_id AND v.id=NEW.sour"
                "ce_version_id AND w.id=json_extract(NEW.payload,'$.workbook_id') AND w.owner=j"
                "son_extract(NEW.payload,'$.owner_subject') AND w.session_id IS json_extract(NE"
                "W.payload,'$.session_id')) BEGIN SELECT RAISE(ABORT,'remediation relationship'"
                "); END"
            ),
        )
        for statement in statements:
            db.execute(statement)
        # Original SQLite insertion order is retained where timestamps tie. No new
        # historical context, provenance, report, or ruleset is synthesized.
        db.execute(
            "INSERT INTO record_order(workbook_id,kind,record_id) SELECT workbook_id,'versi"
            "on',id FROM versions ORDER BY rowid"
        )
        db.execute(
            "INSERT INTO record_order(workbook_id,kind,record_id) SELECT v.workbook_id,'aud"
            "it',a.id FROM audits a JOIN versions v ON a.version_id=v.id ORDER BY a.rowid"
        )
        db.execute(
            "INSERT INTO active_operations SELECT a.version_id,'audit',j.id FROM jobs j JOI"
            "N audits a ON j.audit_id=a.id WHERE j.state IN ('QUEUED','RUNNING')"
        )

    @staticmethod
    def _order(db: sqlite3.Connection, wid: str, kind: str, resource_id: str) -> None:
        db.execute(
            "INSERT INTO record_order(workbook_id,kind,record_id) VALUES (?,?,?)",
            (wid, kind, resource_id),
        )

    @staticmethod
    def _reserve_operation(db: sqlite3.Connection, vid: str, kind: str, job_id: str) -> None:
        if db.execute("SELECT 1 FROM active_operations WHERE version_id=?", (vid,)).fetchone():
            raise ProjectException(409, "ACTIVE_OPERATION", "This version has an active operation.")
        db.execute("INSERT INTO active_operations VALUES (?,?,?)", (vid, kind, job_id))

    @staticmethod
    def _consume_fault(db: sqlite3.Connection, job: JobRecord) -> JobRecord:
        if job.session_id is None:
            return job
        session = SQLiteRepository._session(db, job.session_id)
        report_failure = SQLiteRepository._take_fault(db, job.session_id, "report_failures")
        if report_failure:
            return job.model_copy(update={"test_report_failure": True})
        if not session.audit_failures_remaining:
            return job
        SQLiteRepository._save_session(
            db, session.model_copy(update={"audit_failures_remaining": 0})
        )
        return job.model_copy(update={"test_audit_failure": True})

    @staticmethod
    def get_audit_for_job(db: sqlite3.Connection, job: JobRecord) -> AuditRecord:
        row = db.execute("SELECT payload FROM audits WHERE id=?", (job.audit_id,)).fetchone()
        if row is None:
            raise IntegrityError("Job audit missing")
        return AuditRecord.model_validate_json(row[0])

    def get_workbook(self, workbook_id: str) -> WorkbookRecord | None:
        with self._db() as db:
            if self._deleting(db, workbook_id):
                return None
            row = db.execute("SELECT payload FROM workbooks WHERE id=?", (workbook_id,)).fetchone()
            return WorkbookRecord.model_validate_json(row[0]) if row else None

    @staticmethod
    def _ready_version(db: sqlite3.Connection, version: VersionRecord) -> VersionRecord:
        row = db.execute(
            "SELECT payload FROM versions WHERE id=? AND workbook_id=?",
            (version.version_id, version.workbook_id),
        ).fetchone()
        if row is None:
            raise RecordNotFoundError("Version not found")
        current = VersionRecord.model_validate_json(row[0])
        SQLiteRepository._writable(db, current.workbook_id)
        if (
            current.owner_subject != version.owner_subject
            or current.session_id != version.session_id
        ):
            raise IntegrityError("Version ownership changed")
        if current.session_id is not None:
            session = SQLiteRepository._session(db, current.session_id)
            if session.state != SessionState.READY:
                raise SessionBusyError("Namespace is not ready")
            if current.owner_subject not in session.allowed_subjects:
                raise IntegrityError("Subject is outside the namespace")
        return current

    def has_active_operation(self, version_id: str) -> bool:
        with self._db() as db:
            return (
                db.execute(
                    "SELECT 1 FROM active_operations WHERE version_id=?", (version_id,)
                ).fetchone()
                is not None
            )

    def create_reaudit(self, version: VersionRecord, audit: AuditRecord, job: JobRecord) -> None:
        with self._db(write=True) as db:
            current = self._ready_version(db, version)
            if not (
                audit.version_id == job.version_id == current.version_id
                and audit.workbook_id == job.workbook_id == current.workbook_id
                and audit.owner_subject == current.owner_subject
                and audit.session_id == job.session_id == current.session_id
                and audit.audit_id == job.audit_id
                and audit.status == AuditStatus.PROCESSING
                and job.state == JobState.QUEUED
                and job.worker_id is None
                and job.lease_expires_at is None
                and job.queue_deadline > job.created_at
            ):
                raise IntegrityError("Reaudit relationships differ")
            audit = audit.model_copy(
                update={"started_at": self._effective_now(db, current.session_id, audit.started_at)}
            )
            self._reserve_operation(db, current.version_id, "audit", job.job_id)
            db.execute(
                "INSERT INTO audits VALUES (?,?,?)",
                (audit.audit_id, current.version_id, audit.model_dump_json()),
            )
            job = self._consume_fault(db, job)
            db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?)",
                (job.job_id, job.audit_id, job.state, job.model_dump_json()),
            )
            updated = current.model_copy(
                update={"status": VersionStatus.PROCESSING, "latest_audit_id": audit.audit_id}
            )
            db.execute(
                "UPDATE versions SET payload=? WHERE id=?",
                (updated.model_dump_json(), current.version_id),
            )
            self._order(db, current.workbook_id, "audit", audit.audit_id)

    def history_records(self, workbook_id: str) -> tuple[list[VersionRecord], list[AuditRecord]]:
        with self._db() as db:
            self._writable(db, workbook_id)
            versions = [
                VersionRecord.model_validate_json(row[0])
                for row in db.execute(
                    (
                        "SELECT v.payload FROM versions v JOIN record_order o ON o.kind='versio"
                        "n' AND o"
                        ".record_id=v.id WHERE o.workbook_id=? ORDER BY o.sequence"
                    ),
                    (workbook_id,),
                )
            ]
            audits = [
                AuditRecord.model_validate_json(row[0])
                for row in db.execute(
                    (
                        "SELECT a.payload FROM audits a JOIN record_order o ON o.kind='audit' A"
                        "ND o.rec"
                        "ord_id=a.id WHERE o.workbook_id=? ORDER BY o.sequence"
                    ),
                    (workbook_id,),
                )
            ]
            return versions, audits

    def get_report(self, audit_id: str) -> bytes | None:
        with self._db() as db:
            row = db.execute("SELECT content FROM reports WHERE audit_id=?", (audit_id,)).fetchone()
            return bytes(row[0]) if row else None

    @staticmethod
    def _key_response(
        db: sqlite3.Connection,
        owner: str,
        namespace: str,
        method: str,
        path: str,
        key: str,
        digest: str,
    ) -> RemediationAccepted | None:
        row = db.execute(
            (
                "SELECT digest,response FROM idempotency WHERE owner=? AND namespace=? AND meth"
                "od=? AND path=? AND key=?"
            ),
            (owner, namespace, method, path, key),
        ).fetchone()
        if row:
            if row[0] != digest:
                raise ProjectException(
                    409, "IDEMPOTENCY_CONFLICT", "The key was used for a different request."
                )
            return RemediationAccepted.model_validate_json(row[1])
        return None

    def find_idempotency(
        self, owner: str, namespace: str, method: str, path: str, key: str, digest: str
    ) -> RemediationAccepted | None:
        with self._db() as db:
            return self._key_response(db, owner, namespace, method, path, key, digest)

    def create_remediation(
        self,
        version: VersionRecord,
        remediation: RemediationRecord,
        job: RemediationJob,
        key: str,
        digest: str,
        path: str,
    ) -> RemediationAccepted:
        namespace = version.session_id or "ordinary"
        with self._db(write=True) as db:
            current = self._ready_version(db, version)
            replay = self._key_response(
                db, current.owner_subject, namespace, "POST", path, key, digest
            )
            if replay:
                return replay
            remediation = remediation.model_copy(
                update={
                    "created_at": self._effective_now(
                        db, current.session_id, remediation.created_at
                    )
                }
            )
            job = job.model_copy(
                update={
                    "test_remediation_failure": self._take_fault(
                        db, current.session_id, "remediation_failures"
                    )
                }
            )
            self._reserve_operation(db, current.version_id, "remediation", job.job_id)
            if current.latest_audit_id != remediation.source_audit_id:
                raise ProjectException(409, "STALE_AUDIT", "Use the latest accepted audit.")
            source_audit = AuditRecord.model_validate_json(
                db.execute(
                    "SELECT payload FROM audits WHERE id=?", (remediation.source_audit_id,)
                ).fetchone()[0]
            )
            if source_audit.status in (AuditStatus.PROCESSING, AuditStatus.PROCESSING_FAILED):
                raise ProjectException(
                    409, "AUDIT_NOT_READY", "A successful completed audit is required."
                )
            blob_namespace = f"tests/{current.session_id}" if current.session_id else "ordinary"
            expected_key = f"{blob_namespace}/{current.workbook_id}/{job.output_version_id}/source"
            if not (
                remediation.source_version_id == job.version_id == current.version_id
                and remediation.remediation_id == job.remediation_id
                and remediation.workbook_id == job.workbook_id == current.workbook_id
                and remediation.session_id == job.session_id == current.session_id
                and remediation.owner_subject == current.owner_subject
                and remediation.status == "REMEDIATING"
                and remediation.output_id is None
                and remediation.output_version_id is None
                and remediation.audit_id is None
                and remediation.error is None
                and job.state == JobState.QUEUED
                and job.worker_id is None
                and job.lease_expires_at is None
                and job.queue_deadline > job.created_at
                and job.blob_key == expected_key
            ):
                raise IntegrityError("Remediation relationships differ")
            if (
                db.execute(
                    "SELECT 1 FROM upload_staging WHERE version_id=? OR blob_key=?",
                    (job.output_version_id, job.blob_key),
                ).fetchone()
                or db.execute(
                    "SELECT 1 FROM versions WHERE id=? OR blob_key=?",
                    (job.output_version_id, job.blob_key),
                ).fetchone()
                or db.execute("SELECT 1 FROM audits WHERE id=?", (job.output_audit_id,)).fetchone()
                or db.execute(
                    "SELECT 1 FROM remediation_jobs WHERE json_extract(payload,'$.output_versio"
                    "n_id')=? OR json_extract(payload,'$.output_audit_id')=?",
                    (job.output_version_id, job.output_audit_id),
                ).fetchone()
            ):
                raise IntegrityError("Output identity is already reserved or committed")
            db.execute(
                "INSERT INTO remediations VALUES (?,?,?,?)",
                (
                    remediation.remediation_id,
                    current.version_id,
                    remediation.source_audit_id,
                    remediation.model_dump_json(),
                ),
            )
            db.execute(
                (
                    "INSERT INTO remediation_jobs(id,remediation_id,state,blob_key,payload) VALUES "
                    "(?,?,?,?,?)"
                ),
                (
                    job.job_id,
                    remediation.remediation_id,
                    job.state,
                    job.blob_key,
                    job.model_dump_json(),
                ),
            )
            output = OutputRecord(
                output_id=job.output_id,
                workbook_id=current.workbook_id,
                version_id=job.output_version_id,
                remediation_id=remediation.remediation_id,
                owner_subject=current.owner_subject,
                session_id=current.session_id,
                published=False,
            )
            db.execute(
                "INSERT INTO outputs VALUES (?,?,?,?,?)",
                (
                    output.output_id,
                    current.workbook_id,
                    None,
                    remediation.remediation_id,
                    output.model_dump_json(),
                ),
            )
            response = RemediationAccepted(
                remediationId=remediation.remediation_id,
                sourceVersionId=current.version_id,
                status="REMEDIATING",
            )
            db.execute(
                "INSERT INTO idempotency VALUES (?,?,?,?,?,?,?,?)",
                (
                    current.owner_subject,
                    namespace,
                    "POST",
                    path,
                    key,
                    current.workbook_id,
                    digest,
                    response.model_dump_json(),
                ),
            )
            return response

    def get_remediation(self, remediation_id: str) -> RemediationRecord | None:
        with self._db() as db:
            row = db.execute(
                "SELECT payload FROM remediations WHERE id=?", (remediation_id,)
            ).fetchone()
            if row is None:
                return None
            record = RemediationRecord.model_validate_json(row[0])
            return None if self._deleting(db, record.workbook_id) else record

    def get_output(self, output_id: str) -> OutputRecord | None:
        with self._db() as db:
            row = db.execute("SELECT payload FROM outputs WHERE id=?", (output_id,)).fetchone()
            if row is None:
                return None
            record = OutputRecord.model_validate_json(row[0])
            return None if self._deleting(db, record.workbook_id) else record

    @staticmethod
    def _remediation_job(db: sqlite3.Connection, job_id: str) -> RemediationJob:
        row = db.execute("SELECT payload FROM remediation_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise RecordNotFoundError("Remediation job not found")
        return RemediationJob.model_validate_json(row[0])

    @staticmethod
    def _save_remediation_job(db: sqlite3.Connection, job: RemediationJob) -> None:
        db.execute(
            "UPDATE remediation_jobs SET state=?,payload=? WHERE id=?",
            (job.state, job.model_dump_json(), job.job_id),
        )

    def claim_next_remediation(
        self, worker_id: str, now: datetime, lease_seconds: int
    ) -> RemediationJob | None:
        if lease_seconds <= 0 or not worker_id:
            raise ValueError("Worker and positive lease required")
        with self._db(write=True) as db:
            self._recover(db, now)
            row = db.execute(
                "SELECT payload FROM remediation_jobs WHERE state='QUEUED' ORDER BY rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            job = RemediationJob.model_validate_json(row[0]).model_copy(
                update={
                    "state": JobState.RUNNING,
                    "worker_id": worker_id,
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                }
            )
            self._save_remediation_job(db, job)
            return job

    @staticmethod
    def _live_remediation(job: RemediationJob, worker_id: str, now: datetime) -> None:
        if (
            job.state != JobState.RUNNING
            or job.worker_id != worker_id
            or job.lease_expires_at is None
            or job.lease_expires_at <= now
        ):
            raise LeaseConflictError("Worker has no live remediation lease")

    def register_candidate(self, job_id: str, worker_id: str, digest: str) -> None:
        with self._db(write=True) as db:
            job = self._remediation_job(db, job_id)
            self._live_remediation(job, worker_id, datetime.now(UTC))
            db.execute(
                "UPDATE remediation_jobs SET candidate_sha256=? WHERE id=?", (digest, job_id)
            )

    def reserve_upload_blob(self, version: VersionRecord, deadline: datetime) -> None:
        with self._db(write=True) as db:
            self._writable(db, version.workbook_id)
            if (
                db.execute(
                    "SELECT 1 FROM versions WHERE id=? OR blob_key=?",
                    (version.version_id, version.blob_key),
                ).fetchone()
                or db.execute(
                    "SELECT 1 FROM remediation_jobs WHERE blob_key=? OR json_extract(payload,'$"
                    ".output_version_id')=?",
                    (version.blob_key, version.version_id),
                ).fetchone()
            ):
                raise IntegrityError("Source key already belongs to a version")
            if (
                version.session_id is not None
                and self._session(db, version.session_id).state != SessionState.READY
            ):
                raise SessionBusyError("Session is not ready")
            db.execute(
                "INSERT INTO upload_staging VALUES (?,?,?,?,?)",
                (
                    version.version_id,
                    version.blob_key,
                    version.source_sha256,
                    version.session_id,
                    deadline.isoformat(),
                ),
            )

    def abandon_upload_blob(self, version_id: str) -> None:
        with self._db(write=True) as db:
            db.execute(
                "UPDATE upload_staging SET deadline='1970-01-01T00:00:00+00:00' WHERE version_id=?",
                (version_id,),
            )

    def pending_blob_cleanup(self) -> list[tuple[str, str]]:
        with self._db() as db:
            failed = [
                (row[0], row[1])
                for row in db.execute(
                    "SELECT j.blob_key,j.candidate_sha256 FROM remediation_jobs j "
                    "LEFT JOIN versions v ON v.blob_key=j.blob_key WHERE j.state='FAILED' "
                    "AND j.candidate_sha256 IS NOT NULL AND v.id IS NULL"
                )
            ]
            staged = [
                (row[0], row[1])
                for row in db.execute(
                    "SELECT s.blob_key,s.digest FROM upload_staging s LEFT JOIN versions v ON s"
                    ".version_id=v.id WHERE v.id IS NULL AND s.deadline<=?",
                    (datetime.now(UTC).isoformat(),),
                )
            ]
            retired = [
                (row[0], row[1]) for row in db.execute("SELECT blob_key,digest FROM retired_blobs")
            ]
            return failed + staged + retired

    @staticmethod
    def _provenance(
        db: sqlite3.Connection, version_id: str
    ) -> tuple[RemediationRecord | None, AuditRecord | None]:
        row = db.execute(
            (
                "SELECT r.payload FROM remediations r JOIN outputs o ON o.remediation_id=r.id W"
                "HERE o.version_id=?"
            ),
            (version_id,),
        ).fetchone()
        if row is None:
            return None, None
        remediation = RemediationRecord.model_validate_json(row[0])
        source = db.execute(
            "SELECT payload FROM audits WHERE id=?", (remediation.source_audit_id,)
        ).fetchone()
        return remediation, AuditRecord.model_validate_json(source[0])

    def complete_remediation(
        self,
        job_id: str,
        worker_id: str,
        version: VersionRecord,
        audit: AuditRecord,
        evaluation: AuditEvaluation,
        completed_at: datetime,
    ) -> None:
        with self._db(write=True) as db:
            job = self._remediation_job(db, job_id)
            self._live_remediation(job, worker_id, completed_at)
            remediation = RemediationRecord.model_validate_json(
                db.execute(
                    "SELECT payload FROM remediations WHERE id=?", (job.remediation_id,)
                ).fetchone()[0]
            )
            source = VersionRecord.model_validate_json(
                db.execute("SELECT payload FROM versions WHERE id=?", (job.version_id,)).fetchone()[
                    0
                ]
            )
            registered_hash = db.execute(
                "SELECT candidate_sha256 FROM remediation_jobs WHERE id=?", (job_id,)
            ).fetchone()[0]
            if not (
                remediation.status == "REMEDIATING"
                and version.version_id == job.output_version_id
                and audit.audit_id == version.latest_audit_id == job.output_audit_id
                and audit.version_id == version.version_id
                and audit.workbook_id == version.workbook_id == job.workbook_id
                and version.source_version_id == source.version_id
                and version.owner_subject == audit.owner_subject == source.owner_subject
                and version.session_id == audit.session_id == source.session_id == job.session_id
                and version.blob_key == job.blob_key
                and version.source_sha256 == registered_hash
                and audit.ruleset_version == remediation.ruleset_version
                and version.validated is not None
                and version.source_sha256 == version.validated.source_sha256
                and version.size_bytes == version.validated.size_bytes <= 25_000_000
                and version.worksheet_count == version.validated.worksheet_count <= 50
                and version.nonempty_cell_count == version.validated.nonempty_cell_count <= 250_000
                and version.status in (VersionStatus.PROCESSING, VersionStatus.REMEDIATED)
                and audit.status == AuditStatus.PROCESSING
            ):
                raise IntegrityError("Published output provenance differs")
            effective = self._effective_now(db, job.session_id, completed_at)
            version = version.model_copy(update={"created_at": max(version.created_at, effective)})
            updated_audit = make_audit_result(
                audit, evaluation, None, max(effective, audit.started_at)
            )
            updated_version = version.model_copy(update={"status": VersionStatus.REMEDIATED})
            updated_remediation = remediation.model_copy(
                update={
                    "status": "REMEDIATED",
                    "output_version_id": version.version_id,
                    "output_id": job.output_id,
                    "audit_id": audit.audit_id,
                }
            )
            source_audit = AuditRecord.model_validate_json(
                db.execute(
                    "SELECT payload FROM audits WHERE id=?", (remediation.source_audit_id,)
                ).fetchone()[0]
            )
            content = report_snapshot(
                updated_version, updated_audit, updated_remediation, source_audit
            )
            db.execute(
                "INSERT INTO versions VALUES (?,?,?,?)",
                (
                    version.version_id,
                    version.workbook_id,
                    version.blob_key,
                    updated_version.model_dump_json(),
                ),
            )
            db.execute(
                "INSERT INTO audits VALUES (?,?,?)",
                (audit.audit_id, version.version_id, updated_audit.model_dump_json()),
            )
            db.execute("INSERT INTO reports VALUES (?,?)", (audit.audit_id, content))
            output = OutputRecord(
                output_id=job.output_id,
                workbook_id=version.workbook_id,
                version_id=version.version_id,
                remediation_id=remediation.remediation_id,
                owner_subject=version.owner_subject,
                session_id=version.session_id,
                published=True,
            )
            db.execute(
                "UPDATE outputs SET version_id=?,payload=? WHERE id=?",
                (version.version_id, output.model_dump_json(), job.output_id),
            )
            db.execute(
                "UPDATE remediations SET payload=? WHERE id=?",
                (updated_remediation.model_dump_json(), remediation.remediation_id),
            )
            self._save_remediation_job(db, job.model_copy(update={"state": JobState.DONE}))
            db.execute(
                "DELETE FROM active_operations WHERE version_id=? AND operation_id=?",
                (job.version_id, job.job_id),
            )
            self._order(db, job.workbook_id, "version", version.version_id)
            self._order(db, job.workbook_id, "audit", audit.audit_id)

    def _fail_remediation(
        self, db: sqlite3.Connection, job: RemediationJob, error: ProjectError
    ) -> None:
        remediation = RemediationRecord.model_validate_json(
            db.execute(
                "SELECT payload FROM remediations WHERE id=?", (job.remediation_id,)
            ).fetchone()[0]
        )
        if remediation.status != "REMEDIATING":
            raise LeaseConflictError("Remediation is terminal")
        failed = remediation.model_copy(update={"status": "PROCESSING_FAILED", "error": error})
        db.execute(
            "UPDATE remediations SET payload=? WHERE id=?",
            (failed.model_dump_json(), remediation.remediation_id),
        )
        self._save_remediation_job(db, job.model_copy(update={"state": JobState.FAILED}))
        db.execute(
            "DELETE FROM active_operations WHERE version_id=? AND operation_id=?",
            (job.version_id, job.job_id),
        )

    def fail_remediation(
        self, job_id: str, worker_id: str, error: ProjectError, completed_at: datetime
    ) -> None:
        with self._db(write=True) as db:
            job = self._remediation_job(db, job_id)
            self._live_remediation(job, worker_id, completed_at)
            self._fail_remediation(db, job, error)

    def _recover_remediations(self, db: sqlite3.Connection, now: datetime) -> int:
        count = 0
        for row in db.execute(
            "SELECT payload FROM remediation_jobs WHERE state IN ('QUEUED','RUNNING')"
        ).fetchall():
            job = RemediationJob.model_validate_json(row[0])
            if (job.state == JobState.QUEUED and job.queue_deadline <= now) or (
                job.state == JobState.RUNNING
                and (job.lease_expires_at is None or job.lease_expires_at <= now)
            ):
                error = ProjectError(
                    code="PROCESSING_TIMEOUT" if job.state == JobState.QUEUED else "WORKER_LOST",
                    message="Remediation deadline expired.",
                    requestId=job.job_id,
                    details={"stage": "queue" if job.state == JobState.QUEUED else "worker"},
                )
                self._fail_remediation(db, job, error)
                count += 1
        return count

    @staticmethod
    def _migrate_phase5(db: sqlite3.Connection) -> None:
        # Existing payloads and immutable report bytes are untouched.
        for statement in (
            "CREATE TABLE schedules (workbook_id TEXT PRIMARY KEY REFERENCES workbooks(id), "
            "session_id TEXT, payload TEXT NOT NULL, deadline_since TEXT NOT NULL)",
            "CREATE TABLE reminder_occurrences (workbook_id TEXT REFERENCES workbooks(id), "
            "session_id TEXT, deadline TEXT, offset INTEGER, PRIMARY "
            "KEY(workbook_id,deadline,offset))",
            "CREATE TABLE notifications (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
            "workbook_id TEXT REFERENCES workbooks(id), session_id TEXT, "
            "scheduled_for TEXT, payload TEXT)",
            "CREATE TABLE deletions (workbook_id TEXT PRIMARY KEY, session_id "
            "TEXT, owner TEXT NOT NULL)",
            "CREATE TABLE deleted_workbooks (workbook_id TEXT PRIMARY KEY)",
            "CREATE INDEX notification_order ON notifications(workbook_id,scheduled_for,sequence)",
        ):
            db.execute(statement)

    @staticmethod
    def _deleting(db: sqlite3.Connection, wid: str) -> bool:
        return (
            db.execute("SELECT 1 FROM deletions WHERE workbook_id=?", (wid,)).fetchone() is not None
        )

    @staticmethod
    def _writable(db: sqlite3.Connection, wid: str) -> None:
        if (
            SQLiteRepository._deleting(db, wid)
            or db.execute("SELECT 1 FROM deleted_workbooks WHERE workbook_id=?", (wid,)).fetchone()
        ):
            raise ProjectException(404, "NOT_FOUND", "Workbook is unavailable.")

    @staticmethod
    def _effective_now(db: sqlite3.Connection, sid: str | None, real: datetime) -> datetime:
        if sid is None:
            return real
        session = SQLiteRepository._session(db, sid)
        return session.clock_current_time or real

    def effective_now(self, session_id: str | None) -> datetime:
        with self._db() as db:
            return self._effective_now(db, session_id, datetime.now(UTC))

    @staticmethod
    def _take_fault(db: sqlite3.Connection, sid: str | None, key: str) -> bool:
        if sid is None:
            return False
        session = SQLiteRepository._session(db, sid)
        if not session.faults_remaining.get(key):
            return False
        remaining = dict(session.faults_remaining)
        remaining[key] = 0
        SQLiteRepository._save_session(
            db, session.model_copy(update={"faults_remaining": remaining})
        )
        return True

    def consume_storage_fault(self, session_id: str | None) -> bool:
        with self._db(write=True) as db:
            return self._take_fault(db, session_id, "storage_failures")

    @staticmethod
    def _newest(db: sqlite3.Connection, wid: str) -> VersionRecord:
        row = db.execute(
            "SELECT v.payload FROM versions v JOIN record_order o ON o.record_id=v.id "
            "AND o.kind='version' WHERE v.workbook_id=? ORDER BY o.sequence DESC LIMIT 1",
            (wid,),
        ).fetchone()
        if row is None:
            raise ProjectException(404, "NOT_FOUND", "Stored version is unavailable.")
        return VersionRecord.model_validate_json(row[0])

    @staticmethod
    def _schedule(db: sqlite3.Connection, wid: str) -> ScheduleResponse:
        SQLiteRepository._writable(db, wid)
        version = SQLiteRepository._newest(db, wid)
        row = db.execute("SELECT payload FROM schedules WHERE workbook_id=?", (wid,)).fetchone()
        if row:
            return ScheduleResponse.model_validate_json(row[0]).model_copy(
                update={"versionId": version.version_id}
            )
        return ScheduleResponse(
            workbookId=wid,
            enabled=False,
            intervalDays=None,
            complianceDeadline=None,
            versionId=version.version_id,
            nextAuditAt=None,
            reminderDays=(90, 30, 7),
        )

    def get_schedule(self, workbook_id: str) -> ScheduleResponse:
        with self._db() as db:
            return self._schedule(db, workbook_id)

    def configure_schedule(self, workbook_id: str, request: ScheduleRequest) -> ScheduleResponse:
        with self._db(write=True) as db:
            previous = self._schedule(db, workbook_id)
            version = self._newest(db, workbook_id)
            now = self._effective_now(db, version.session_id, datetime.now(UTC))
            next_due = previous.nextAuditAt
            if not request.enabled:
                next_due = None
            elif not previous.enabled or request.intervalDays != previous.intervalDays:
                assert request.intervalDays is not None
                next_due = add_days(now, request.intervalDays)
            response = ScheduleResponse(
                **(
                    request.model_dump()
                    | {
                        "workbookId": workbook_id,
                        "versionId": version.version_id,
                        "nextAuditAt": next_due,
                        "reminderDays": (90, 30, 7),
                    }
                )
            )
            old = db.execute(
                "SELECT deadline_since FROM schedules WHERE workbook_id=?", (workbook_id,)
            ).fetchone()
            since = (
                old[0]
                if old and previous.complianceDeadline == request.complianceDeadline
                else now.isoformat()
            )
            db.execute(
                "INSERT INTO schedules VALUES (?,?,?,?) ON CONFLICT(workbook_id) DO UPDATE SET "
                "payload=excluded.payload,deadline_since=excluded.deadline_since",
                (workbook_id, version.session_id, response.model_dump_json(), since),
            )
            return response

    def get_notifications(self, workbook_id: str) -> NotificationResponse:
        with self._db() as db:
            self._writable(db, workbook_id)
            records = [
                Notification.model_validate_json(row[0])
                for row in db.execute(
                    "SELECT payload FROM notifications WHERE workbook_id=? ORDER BY "
                    "scheduled_for,sequence",
                    (workbook_id,),
                )
            ]
            return NotificationResponse(notifications=records)

    def process_schedules(
        self,
        current_time: datetime,
        *,
        session_id: str | None = None,
        ruleset_version: str = "2.0.0",
        queue_seconds: float = 15,
        owner_subject: str | None = None,
    ) -> ClockResponse:
        """One durable claim transaction. Test clock callers supply the creating admin subject.

        Ordinary ticks skip busy versions; isolated advances reject the entire work set.
        Queue/lease timestamps are always real UTC, independently of effective calendar time.
        """
        with self._db(write=True) as db:
            session = None
            if session_id is not None:
                session = self._session(db, session_id)
                if session.owner_subject != owner_subject:
                    raise ProjectException(
                        403, "FORBIDDEN", "Session belongs to another administrator."
                    )
                if session.state != SessionState.READY:
                    raise ProjectException(409, "SESSION_NOT_READY", "Test session is not ready.")
                if session.clock_current_time is None:
                    raise ProjectException(
                        409, "LEGACY_SESSION_CLOCK", "Prepare a fresh clock session."
                    )
                if current_time < session.clock_current_time:
                    raise ProjectException(400, "CLOCK_REWIND", "Test time cannot move backward.")
                if db.execute(
                    "SELECT 1 FROM deletions WHERE session_id=?", (session_id,)
                ).fetchone():
                    raise ProjectException(
                        409, "DELETION_IN_PROGRESS", "Workbook cleanup is pending."
                    )
            rows = db.execute(
                "SELECT workbook_id,payload,deadline_since FROM schedules "
                "WHERE session_id IS ? AND workbook_id NOT IN (SELECT workbook_id FROM deletions)",
                (session_id,),
            ).fetchall()
            audits: list[tuple[datetime, str, ScheduleResponse, VersionRecord]] = []
            reminders: list[tuple[datetime, str, int, str, VersionRecord, bool]] = []
            for wid, payload, since in rows:
                policy = ScheduleResponse.model_validate_json(payload)
                version = self._newest(db, wid)
                # Capture eligibility before this transaction accepts any new audit.
                audit_row = db.execute(
                    "SELECT payload FROM audits WHERE id=?", (version.latest_audit_id,)
                ).fetchone()
                audit = AuditRecord.model_validate_json(audit_row[0]) if audit_row else None
                eligible = (
                    audit is None
                    or audit.status in (AuditStatus.PROCESSING, AuditStatus.PROCESSING_FAILED)
                    or bool(audit.findings)
                )
                if (
                    policy.enabled
                    and policy.nextAuditAt is not None
                    and policy.nextAuditAt <= current_time
                ):
                    busy = db.execute(
                        "SELECT 1 FROM active_operations WHERE version_id=?", (version.version_id,)
                    ).fetchone()
                    if busy and session_id is not None:
                        raise ProjectException(
                            409, "ACTIVE_OPERATION", "A due version has active work."
                        )
                    if not busy:
                        audits.append((policy.nextAuditAt, wid, policy, version))
                if policy.complianceDeadline:
                    for offset in (90, 30, 7):
                        due = reminder_time(policy.complianceDeadline, offset)
                        if (
                            due is not None
                            and datetime.fromisoformat(since) <= due <= current_time
                            and not db.execute(
                                "SELECT 1 FROM reminder_occurrences WHERE workbook_id=? "
                                "AND deadline=? "
                                "AND offset=?",
                                (wid, policy.complianceDeadline, offset),
                            ).fetchone()
                        ):
                            reminders.append(
                                (due, wid, offset, policy.complianceDeadline, version, eligible)
                            )
            if session_id is not None and (len(audits) > 1000 or len(reminders) > 3000):
                raise ProjectException(
                    409, "CLOCK_WORK_LIMIT", "Scheduled work exceeds the atomic processing limit."
                )
            if session_id is None:
                audits = sorted(audits, key=lambda a: (a[0], a[1]))[:1000]
                reminders = sorted(reminders, key=lambda r: (r[0], r[1], -r[2]))[:3000]
            # Precompute every calendar result before mutating anything (including faults).
            plans = []
            for due, wid, policy, version in sorted(audits, key=lambda a: (a[0], a[1])):
                assert policy.intervalDays is not None
                interval = timedelta(days=policy.intervalDays)
                steps = (current_time - due) // interval + 1
                next_due = add_days(due, steps * policy.intervalDays)
                plans.append((due, wid, policy, version, next_due))
            audit_ids: list[str] = []
            notification_ids: list[str] = []
            real = datetime.now(UTC)
            for due, wid, policy, version, next_due in plans:
                aid, jid = uuid4().hex, uuid4().hex
                audit = AuditRecord(
                    audit_id=aid,
                    workbook_id=wid,
                    version_id=version.version_id,
                    owner_subject=version.owner_subject,
                    session_id=session_id,
                    ruleset_version=ruleset_version,
                    status=AuditStatus.PROCESSING,
                    started_at=current_time,
                    completed_at=None,
                    findings=[],
                    limitations=[],
                    error=None,
                )
                job = JobRecord(
                    job_id=jid,
                    audit_id=aid,
                    workbook_id=wid,
                    version_id=version.version_id,
                    session_id=session_id,
                    state=JobState.QUEUED,
                    created_at=real,
                    queue_deadline=real + timedelta(seconds=queue_seconds),
                    worker_id=None,
                    lease_expires_at=None,
                    test_scheduled_audit_failure=self._take_fault(
                        db, session_id, "scheduled_audit_failures"
                    ),
                )
                self._reserve_operation(db, version.version_id, "audit", jid)
                db.execute(
                    "INSERT INTO audits VALUES (?,?,?)",
                    (aid, version.version_id, audit.model_dump_json()),
                )
                db.execute(
                    "INSERT INTO jobs VALUES (?,?,?,?)",
                    (jid, aid, job.state, job.model_dump_json()),
                )
                version = version.model_copy(
                    update={"latest_audit_id": aid, "status": VersionStatus.PROCESSING}
                )
                db.execute(
                    "UPDATE versions SET payload=? WHERE id=?",
                    (version.model_dump_json(), version.version_id),
                )
                self._order(db, wid, "audit", aid)
                policy = policy.model_copy(update={"nextAuditAt": next_due})
                db.execute(
                    "UPDATE schedules SET payload=? WHERE workbook_id=?",
                    (policy.model_dump_json(), wid),
                )
                audit_ids.append(aid)
                logger.info(
                    "scheduled_audit_accepted",
                    extra={
                        "schedulerId": jid,
                        "auditId": aid,
                        "workbookId": wid,
                        "sessionId": session_id,
                        "dispatchLag": (current_time - due).total_seconds(),
                    },
                )
            for due, wid, offset, deadline, version, eligible in sorted(
                reminders, key=lambda r: (r[0], r[1], -r[2])
            ):
                db.execute(
                    "INSERT INTO reminder_occurrences VALUES (?,?,?,?)",
                    (wid, session_id, deadline, offset),
                )
                if not eligible:
                    continue
                failed = self._take_fault(db, session_id, "notification_failures")
                notification = Notification(
                    notificationId=uuid4().hex,
                    workbookId=wid,
                    versionId=version.version_id,
                    scheduledFor=due,
                    status="FAILED" if failed else "DELIVERED",
                    message="Reminder recording failed in the isolated test session."
                    if failed
                    else (
                        f"The supplied compliance deadline is {offset} days away. "
                        "Review the stored workbook findings."
                    ),
                )
                db.execute(
                    "INSERT INTO "
                    "notifications(workbook_id,session_id,scheduled_for,payload) VALUES "
                    "(?,?,?,?)",
                    (
                        wid,
                        session_id,
                        due.isoformat(timespec="microseconds"),
                        notification.model_dump_json(),
                    ),
                )
                notification_ids.append(notification.notificationId)
                logger.log(
                    logging.ERROR if failed else logging.INFO,
                    "notification_recorded",
                    extra={
                        "workbookId": wid,
                        "sessionId": session_id,
                        "status": notification.status,
                    },
                )
            if session is not None:
                # Fault consumption may have updated this same session payload.
                session = self._session(db, session.session_id)
                self._save_session(
                    db,
                    session.model_copy(
                        update={
                            "clock_current_time": current_time,
                            "deployment_protected_until": max(
                                session.deployment_protected_until or real,
                                real + timedelta(seconds=DEPLOYMENT_PROTECTION_SECONDS),
                            ),
                        }
                    ),
                )
            return ClockResponse(
                currentTime=current_time,
                triggeredAudits=audit_ids,
                triggeredNotifications=notification_ids,
            )

    def deletion_owner(self, workbook_id: str) -> tuple[str, str | None] | None:
        with self._db() as db:
            row = db.execute(
                "SELECT owner,session_id FROM deletions WHERE workbook_id=?", (workbook_id,)
            ).fetchone()
            return (row[0], row[1]) if row else None

    def begin_workbook_deletion(
        self, workbook_id: str, owner: str, session_id: str | None
    ) -> list[str]:
        with self._db(write=True) as db:
            pending = db.execute(
                "SELECT owner,session_id FROM deletions WHERE workbook_id=?", (workbook_id,)
            ).fetchone()
            if pending:
                if tuple(pending) != (owner, session_id):
                    raise ProjectException(404, "NOT_FOUND", "Workbook is unavailable.")
            else:
                row = db.execute(
                    "SELECT owner,session_id FROM workbooks WHERE id=?", (workbook_id,)
                ).fetchone()
                if row is None or row[1] != session_id:
                    raise ProjectException(404, "NOT_FOUND", "Workbook is unavailable.")
                if row[0] != owner:
                    raise ProjectException(
                        403, "FORBIDDEN", "Only the owner may delete this workbook."
                    )
                self._recover(db, datetime.now(UTC))
                if db.execute(
                    "SELECT 1 FROM active_operations a JOIN versions v ON a.version_id=v.id "
                    "WHERE v.workbook_id=?",
                    (workbook_id,),
                ).fetchone():
                    raise ProjectException(409, "ACTIVE_OPERATION", "Workbook has active work.")
                prefix = f"tests/{session_id}" if session_id else "ordinary"
                if db.execute(
                    "SELECT 1 FROM upload_staging WHERE blob_key LIKE ? AND "
                    "julianday(deadline)>julianday(?)",
                    (f"{prefix}/{workbook_id}/%", datetime.now(UTC).isoformat()),
                ).fetchone():
                    raise ProjectException(409, "ACTIVE_OPERATION", "Workbook upload is active.")
                if self._take_fault(db, session_id, "deletion_failures"):
                    # Commit consumption, then report failure without reserving/removing anything.
                    return ["TEST_DELETION_FAILURE"]
                db.execute("INSERT INTO deletions VALUES (?,?,?)", (workbook_id, session_id, owner))
            return self._workbook_blob_keys(db, workbook_id, session_id)

    @staticmethod
    def _workbook_blob_keys(db: sqlite3.Connection, wid: str, sid: str | None) -> list[str]:
        prefix = f"tests/{sid}" if sid else "ordinary"
        return [
            row[0]
            for row in db.execute(
                "SELECT blob_key FROM versions WHERE workbook_id=? UNION "
                "SELECT j.blob_key FROM remediation_jobs j JOIN remediations r ON "
                "j.remediation_id=r.id "
                "JOIN versions v ON r.source_version_id=v.id WHERE v.workbook_id=? UNION "
                "SELECT blob_key FROM upload_staging WHERE blob_key LIKE ? UNION "
                "SELECT blob_key FROM retired_blobs WHERE blob_key LIKE ?",
                (wid, wid, f"{prefix}/{wid}/%", f"{prefix}/{wid}/%"),
            )
        ]

    @staticmethod
    def _erase_workbook(db: sqlite3.Connection, wid: str) -> None:
        """FK-ordered erasure also used on registered metadata backup copies."""
        for table in (
            "schedules",
            "reminder_occurrences",
            "notifications",
            "idempotency",
            "outputs",
            "record_order",
        ):
            db.execute(f"DELETE FROM {table} WHERE workbook_id=?", (wid,))
        db.execute(
            "DELETE FROM remediation_jobs WHERE remediation_id IN (SELECT r.id FROM remediations r "
            "JOIN versions v ON r.source_version_id=v.id WHERE v.workbook_id=?)",
            (wid,),
        )
        db.execute(
            "DELETE FROM remediations WHERE source_version_id IN (SELECT id FROM "
            "versions WHERE workbook_id=?)",
            (wid,),
        )
        db.execute(
            "DELETE FROM reports WHERE audit_id IN (SELECT a.id FROM audits a JOIN "
            "versions v ON a.version_id=v.id WHERE v.workbook_id=?)",
            (wid,),
        )
        db.execute(
            "DELETE FROM jobs WHERE audit_id IN (SELECT a.id FROM audits a JOIN "
            "versions v ON a.version_id=v.id WHERE v.workbook_id=?)",
            (wid,),
        )
        db.execute(
            "DELETE FROM active_operations WHERE version_id IN (SELECT id FROM "
            "versions WHERE workbook_id=?)",
            (wid,),
        )
        db.execute(
            "DELETE FROM audits WHERE version_id IN (SELECT id FROM versions WHERE workbook_id=?)",
            (wid,),
        )
        db.execute("DELETE FROM versions WHERE workbook_id=?", (wid,))
        db.execute("DELETE FROM workbooks WHERE id=?", (wid,))
        db.execute("INSERT OR IGNORE INTO deleted_workbooks VALUES (?)", (wid,))

    def finish_workbook_deletion(self, workbook_id: str) -> None:
        with self._db(write=True) as db:
            row = db.execute(
                "SELECT session_id FROM deletions WHERE workbook_id=?", (workbook_id,)
            ).fetchone()
            if row is None:
                raise ProjectException(404, "NOT_FOUND", "Deletion reservation is unavailable.")
            prefix = f"tests/{row[0]}" if row[0] else "ordinary"
            for table in ("upload_staging", "retired_blobs"):
                db.execute(
                    f"DELETE FROM {table} WHERE blob_key LIKE ?", (f"{prefix}/{workbook_id}/%",)
                )
            self._erase_workbook(db, workbook_id)
        self.compact_deleted_content()
        with self._db(write=True) as db:
            db.execute("DELETE FROM deletions WHERE workbook_id=?", (workbook_id,))

    def compact_deleted_content(self) -> None:
        # Remove freed pages and WAL copies before acknowledging deletion. No transaction
        # may hold an old content snapshot; otherwise leave the reservation recoverable.
        with self._db() as db:
            if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                raise StorageError("Content checkpoint is busy")
            db.execute("VACUUM")
            if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                raise StorageError("Content checkpoint is busy")

    def monitoring_snapshot(self) -> dict[str, int]:
        with self._db() as db:
            return {
                "queueDepth": db.execute(
                    "SELECT count(*) FROM jobs WHERE state='QUEUED'"
                ).fetchone()[0]
                + db.execute(
                    "SELECT count(*) FROM remediation_jobs WHERE state='QUEUED'"
                ).fetchone()[0],
                "failedJobs": db.execute(
                    "SELECT count(*) FROM jobs WHERE state='FAILED'"
                ).fetchone()[0]
                + db.execute(
                    "SELECT count(*) FROM remediation_jobs WHERE state='FAILED'"
                ).fetchone()[0],
                "notificationFailures": db.execute(
                    "SELECT count(*) FROM notifications WHERE "
                    "json_extract(payload,'$.status')='FAILED'"
                ).fetchone()[0],
                "pendingDeletions": db.execute("SELECT count(*) FROM deletions").fetchone()[0],
            }

    def record_deletion(self, workbook_id: str) -> None:
        # Deliberately separate from restorable content-bearing database backups.
        journal = self.path.with_suffix(self.path.suffix + ".deletions")
        try:
            with sqlite3.connect(journal, timeout=2) as db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("CREATE TABLE IF NOT EXISTS deleted (workbook_id TEXT PRIMARY KEY)")
                db.execute("INSERT OR IGNORE INTO deleted VALUES (?)", (workbook_id,))
        except sqlite3.Error as exc:
            raise StorageError("Deletion journal unavailable") from exc

    def _restore_deletion_fences(self) -> None:
        journal = self.path.with_suffix(self.path.suffix + ".deletions")
        if not journal.exists():
            return
        try:
            with sqlite3.connect(journal, timeout=2) as ledger:
                deleted = [row[0] for row in ledger.execute("SELECT workbook_id FROM deleted")]
            with self._db(write=True) as db:
                for wid in deleted:
                    db.execute(
                        "INSERT OR IGNORE INTO deletions SELECT id,session_id,owner FROM "
                        "workbooks WHERE id=?",
                        (wid,),
                    )
                    db.execute("INSERT OR IGNORE INTO deleted_workbooks VALUES (?)", (wid,))
        except sqlite3.Error as exc:
            raise StorageError("Deletion journal replay failed") from exc
