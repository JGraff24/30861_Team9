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
from app.services.reports import make_audit_result, report_snapshot

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
            if version > 2:
                raise StorageError("Metadata schema is newer than this release")
            if version == 0:
                for statement in SCHEMA_V1:
                    db.execute(statement)
                db.execute("PRAGMA user_version=1")
            if version < 2:
                self._migrate_phase4(db)
                db.execute("PRAGMA user_version=2")

    def health(self) -> bool:
        try:
            with self._db() as db:
                return db.execute("PRAGMA user_version").fetchone()[0] == 2 and (
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
                from datetime import UTC

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
        updated = make_audit_result(audit, evaluation, error, completed_at)
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
                        details={},
                    ),
                    now,
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
            from datetime import UTC

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
            return RemediationRecord.model_validate_json(row[0]) if row else None

    def get_output(self, output_id: str) -> OutputRecord | None:
        with self._db() as db:
            row = db.execute("SELECT payload FROM outputs WHERE id=?", (output_id,)).fetchone()
            return OutputRecord.model_validate_json(row[0]) if row else None

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
        from datetime import UTC

        with self._db(write=True) as db:
            job = self._remediation_job(db, job_id)
            self._live_remediation(job, worker_id, datetime.now(UTC))
            db.execute(
                "UPDATE remediation_jobs SET candidate_sha256=? WHERE id=?", (digest, job_id)
            )

    def reserve_upload_blob(self, version: VersionRecord, deadline: datetime) -> None:
        with self._db(write=True) as db:
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
        from datetime import UTC

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
            updated_audit = make_audit_result(audit, evaluation, None, completed_at)
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
