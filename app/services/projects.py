"""Project orchestration over the shared owner ports."""

import logging
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from uuid import uuid4

from app.contracts.errors import (
    EngineExecutionError,
    IntegrityError,
    ProjectException,
    StorageError,
)
from app.contracts.models import (
    AuditRecord,
    AuditResponse,
    AuditStatus,
    ExecutionMeasurement,
    JobRecord,
    JobState,
    Principal,
    ProcessingLimits,
    Role,
    SessionState,
    UploadResponse,
    ValidatedWorkbook,
    VersionRecord,
    VersionResponse,
    VersionStatus,
    WorkbookRecord,
)
from app.contracts.ports import SystemServices
from app.jobs.execution import execute_bounded
from app.storage.blobs import sha256

logger = logging.getLogger(__name__)


def identifier() -> str:
    return uuid4().hex


def verify_validation(data: bytes, validated: ValidatedWorkbook, limits: ProcessingLimits) -> None:
    parts = list(validated.worksheet_parts.values())
    if not (
        validated.source_sha256 == sha256(data)
        and validated.size_bytes == len(data) <= limits.max_file_bytes
        and 1 <= validated.worksheet_count <= limits.max_worksheets
        and validated.worksheet_count == len(parts) == len(set(parts))
        and validated.nonempty_cell_count <= limits.max_nonempty_cells
        and all(
            not PurePosixPath(p).is_absolute()
            and ".." not in PurePosixPath(p).parts
            and str(PurePosixPath(p)) == p
            and "\\" not in p
            and ":" not in p
            for p in parts
        )
    ):
        raise ProjectException(500, "ENGINE_RESULT_INVALID", "Processor metadata is inconsistent.")


def record_measurement(
    services: SystemServices,
    session_id: str | None,
    measurement: ExecutionMeasurement | None,
) -> None:
    if session_id is None:
        return
    if services.repository is None:
        raise StorageError("Repository missing")
    if measurement is not None and (
        measurement.session_id != session_id
        or measurement.revision != services.settings.deployment_sha
    ):
        measurement = None
    if measurement is None:
        measurement = ExecutionMeasurement(
            session_id=session_id,
            invocation_id=identifier(),
            revision=services.settings.deployment_sha,
            executable_lines={},
            covered_lines={},
            unavailable_reason="Child instrumentation unavailable for this invocation.",
        )
    services.repository.record_measurement(measurement)


class ProjectService:
    def __init__(self, services: SystemServices) -> None:
        self.services = services

    def check_session(self, session_id: str | None, principal: Principal) -> None:
        if session_id is None:
            return
        services = self.services
        if not services.settings.enable_test_interface:
            raise ProjectException(400, "TEST_INTERFACE_DISABLED", "Testing headers are disabled.")
        if services.repository is None:
            raise ProjectException(503, "SERVICE_UNAVAILABLE", "Metadata service unavailable.")
        session = services.repository.get_test_session(session_id)
        if session is None or session.state == SessionState.CLEANED:
            raise ProjectException(400, "UNKNOWN_TEST_SESSION", "Test session is unknown.")
        if principal.subject not in session.allowed_subjects:
            raise ProjectException(403, "FORBIDDEN", "Identity is outside this test session.")
        if session.state != SessionState.READY:
            raise ProjectException(409, "TEST_SESSION_NOT_READY", "Test session is not ready.")

    def _authorize(
        self, owner: str, resource_session: str | None, session_id: str | None, principal: Principal
    ) -> None:
        self.check_session(session_id, principal)
        if resource_session != session_id:
            raise ProjectException(404, "NOT_FOUND", "Resource not found in this namespace.")
        auth = self.services.auth_service
        if auth is None or not auth.can_read(owner, principal):
            raise ProjectException(403, "FORBIDDEN", "Resource belongs to another owner.")

    def upload(
        self, data: bytes, filename: str, principal: Principal, session_id: str | None
    ) -> UploadResponse:
        services = self.services
        self.check_session(session_id, principal)
        if principal.role != Role.USER:
            raise ProjectException(403, "FORBIDDEN", "A user identity is required for uploads.")
        repository, blobs, runner = services.repository, services.blob_store, services.runner
        if repository is None or blobs is None or runner is None:
            raise ProjectException(503, "SERVICE_UNAVAILABLE", "Workbook processing unavailable.")
        if not filename.strip() or len(filename) > 255 or any(ord(c) < 32 for c in filename):
            raise ProjectException(400, "INVALID_FILENAME", "A valid display filename is required.")
        extension = PurePosixPath(filename).suffix.lower()
        if extension == ".xlsm":
            raise ProjectException(
                422,
                "MACRO_ENABLED_WORKBOOK_UNSUPPORTED",
                "Macro-enabled workbooks are unsupported.",
            )
        if extension != ".xlsx":
            raise ProjectException(
                415, "UNSUPPORTED_FILE_TYPE", "Only .xlsx workbooks are supported."
            )
        limits = ProcessingLimits()
        try:
            result = execute_bounded(
                runner,
                "validate",
                data,
                filename,
                limits,
                services.settings.ruleset_version,
                None,
                session_id,
                services.settings.deployment_sha,
                min(30, services.settings.validation_timeout_seconds),
                services.settings.engine_memory_limit_mib,
            )
        except EngineExecutionError as exc:
            record_measurement(services, session_id, exc.measurement)
            raise
        record_measurement(services, session_id, result.measurement)
        if result.validated is None:
            raise ProjectException(500, "ENGINE_RESULT_INVALID", "Validation result is absent.")
        validated = result.validated
        verify_validation(data, validated, limits)
        now = datetime.now(UTC)
        wid, vid, aid, jid = (identifier() for _ in range(4))
        namespace = f"tests/{session_id}" if session_id else "ordinary"
        key = f"{namespace}/{wid}/{vid}/source"
        workbook = WorkbookRecord(
            workbook_id=wid, owner_subject=principal.subject, session_id=session_id, created_at=now
        )
        version = VersionRecord(
            version_id=vid,
            workbook_id=wid,
            owner_subject=principal.subject,
            session_id=session_id,
            file_name=filename,
            source_sha256=validated.source_sha256,
            blob_key=key,
            size_bytes=validated.size_bytes,
            worksheet_count=validated.worksheet_count,
            nonempty_cell_count=validated.nonempty_cell_count,
            created_at=now,
            status=VersionStatus.PROCESSING,
            latest_audit_id=aid,
            source_version_id=None,
            validated=validated,
        )
        audit = AuditRecord(
            audit_id=aid,
            workbook_id=wid,
            version_id=vid,
            owner_subject=principal.subject,
            session_id=session_id,
            ruleset_version=services.settings.ruleset_version,
            status=AuditStatus.PROCESSING,
            started_at=now,
            completed_at=None,
            findings=[],
            limitations=[],
            error=None,
        )
        job = JobRecord(
            job_id=jid,
            audit_id=aid,
            workbook_id=wid,
            version_id=vid,
            session_id=session_id,
            state=JobState.QUEUED,
            created_at=now,
            queue_deadline=now + timedelta(seconds=services.settings.queue_timeout_seconds),
            worker_id=None,
            lease_expires_at=None,
        )
        # Only roll back a key after our own immutable put succeeded. A collision must not delete.
        stored = False
        try:
            returned_hash = blobs.put_source(key, data)
            stored = True
            if returned_hash != validated.source_sha256:
                raise IntegrityError("Source write checksum differs")
            repository.create_upload(workbook, version, audit, job)
        except Exception:
            if stored:
                try:
                    blobs.delete_uncommitted_source(key, validated.source_sha256)
                except StorageError:
                    logger.error("source_rollback_failed", extra={"jobId": jid, "auditId": aid})
            raise
        return UploadResponse(workbookId=wid, versionId=vid, auditId=aid, status="PROCESSING")

    def _version(
        self, wid: str, vid: str, principal: Principal, session_id: str | None
    ) -> VersionRecord:
        if self.services.repository is None:
            raise ProjectException(503, "SERVICE_UNAVAILABLE", "Metadata service unavailable.")
        version = self.services.repository.get_version(wid, vid)
        if version is None:
            raise ProjectException(404, "NOT_FOUND", "Version not found.")
        self._authorize(version.owner_subject, version.session_id, session_id, principal)
        # Polling also expires queued work when the worker is down. Leased jobs are
        # never replayed; recovery is transactional with claims/completion.
        self.services.repository.recover_stale_jobs(datetime.now(UTC))
        refreshed = self.services.repository.get_version(wid, vid)
        if refreshed is None:
            raise ProjectException(404, "NOT_FOUND", "Version no longer exists.")
        return refreshed

    def get_version(
        self, wid: str, vid: str, principal: Principal, session_id: str | None
    ) -> VersionResponse:
        version = self._version(wid, vid, principal, session_id)
        blobs = self.services.blob_store
        if blobs is None:
            raise ProjectException(503, "SERVICE_UNAVAILABLE", "Source storage unavailable.")
        data = blobs.read_source(version.blob_key)
        if len(data) != version.size_bytes or sha256(data) != version.source_sha256:
            raise IntegrityError("Source integrity differs")
        return VersionResponse(
            workbookId=wid,
            versionId=vid,
            fileName=version.file_name,
            sha256=version.source_sha256,
            createdAt=version.created_at,
            status=version.status,
            latestAuditId=version.latest_audit_id,
            sourceVersionId=version.source_version_id,
        )

    def get_audit(
        self, wid: str, vid: str, aid: str, principal: Principal, session_id: str | None
    ) -> AuditResponse:
        self._version(wid, vid, principal, session_id)
        assert self.services.repository is not None
        audit = self.services.repository.get_audit(wid, vid, aid)
        if audit is None:
            raise ProjectException(404, "NOT_FOUND", "Audit not found.")
        self._authorize(audit.owner_subject, audit.session_id, session_id, principal)
        return AuditResponse(
            auditId=aid,
            workbookId=wid,
            versionId=vid,
            rulesetVersion=audit.ruleset_version,
            status=audit.status,
            startedAt=audit.started_at,
            completedAt=audit.completed_at,
            findings=audit.findings,
            limitations=audit.limitations,
            error=audit.error,
        )
