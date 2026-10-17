"""SQLite metadata, durable queue, test namespaces and real line-set aggregation."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from app.contracts.errors import (
    DuplicateIdentifierError,
    IntegrityError,
    LeaseConflictError,
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
    "invocation_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(session_id, invocation_id))",
    "CREATE INDEX queue_state ON jobs(state)",
    "CREATE INDEX workbook_session ON workbooks(session_id)",
)


class SQLiteRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _db(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA synchronous=FULL")
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
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
        with self._db(write=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise StorageError("Metadata schema is newer than this release")
            if version == 0:
                for statement in SCHEMA_V1:
                    db.execute(statement)
                db.execute("PRAGMA user_version=1")

    def health(self) -> bool:
        try:
            with self._db() as db:
                return db.execute("PRAGMA user_version").fetchone()[0] == 1 and (
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
            if workbook.session_id is not None:
                session = self._session(db, workbook.session_id)
                if session.state != SessionState.READY:
                    raise SessionBusyError("Test session is not ready")
                if workbook.owner_subject not in session.allowed_subjects:
                    raise IntegrityError("Subject is not allowed in this test namespace")
            db.execute(
                "INSERT INTO workbooks VALUES (?,?,?,?)",
                (
                    workbook.workbook_id,
                    workbook.session_id,
                    workbook.owner_subject,
                    workbook.model_dump_json(),
                ),
            )
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

    def get_version(self, workbook_id: str, version_id: str) -> VersionRecord | None:
        with self._db() as db:
            row = db.execute(
                "SELECT payload FROM versions WHERE id=? AND workbook_id=?",
                (version_id, workbook_id),
            ).fetchone()
            return VersionRecord.model_validate_json(row[0]) if row else None

    def get_audit(self, workbook_id: str, version_id: str, audit_id: str) -> AuditRecord | None:
        with self._db() as db:
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
        findings = (
            sorted(evaluation.findings, key=lambda f: (f.ruleId, f.worksheet, f.target))
            if evaluation
            else []
        )
        if len({f.findingId for f in findings}) != len(findings):
            raise IntegrityError("Duplicate finding identifiers")
        status = (
            "PROCESSING_FAILED" if error else ("REVIEW_REQUIRED" if findings else "AUDIT_COMPLETE")
        )
        updated = AuditRecord.model_validate(
            audit.model_dump()
            | {
                "status": status,
                "completed_at": completed_at,
                "findings": findings,
                "limitations": evaluation.limitations if evaluation else [],
                "error": error,
            }
        )
        updated_version = VersionRecord.model_validate(version.model_dump() | {"status": status})
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
                code = "QUEUE_TIMEOUT" if job.state == JobState.QUEUED else "WORKER_LEASE_EXPIRED"
                self._terminal(
                    db,
                    job,
                    None,
                    ProjectError(
                        code=code,
                        message="Processing deadline expired.",
                        requestId=job.job_id,
                        details={},
                    ),
                    now,
                )
                count += 1
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
            if rows:
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
            return [
                row[0]
                for row in db.execute(
                    "SELECT v.blob_key FROM versions v JOIN workbooks w ON v.workbook_id=w.id "
                    "WHERE w.session_id=?",
                    (session_id,),
                )
            ]

    def finish_session_cleanup(self, session_id: str) -> None:
        with self._db(write=True) as db:
            session = self._session(db, session_id)
            if session.state != SessionState.CLEANED:
                raise SessionBusyError("Cleanup has not been reserved")
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
        basis = "Validation/audit child line sets only; not whole-system coverage."
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
