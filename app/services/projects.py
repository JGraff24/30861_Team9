"""Project orchestration over the shared owner ports."""

import logging
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

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
    Identifier,
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
from app.contracts.phase4 import (
    HistoryAudit,
    HistoryResponse,
    HistoryVersion,
    ReauditRequest,
    ReauditResponse,
    RemediationAccepted,
    RemediationJob,
    RemediationRecord,
    RemediationResponse,
    ReviewResponse,
)
from app.contracts.ports import SystemServices
from app.jobs.execution import execute_bounded
from app.jobs.remediation import require_correction_engine, validate_decisions_bounded
from app.services.decisions import parse_remediation_request, validate_decisions
from app.services.reports import bounded_json, canonical_request
from app.storage.blobs import sha256

logger = logging.getLogger(__name__)


def identifier() -> str:
    return uuid4().hex


def verify_validation(data: bytes, validated: ValidatedWorkbook, limits: ProcessingLimits) -> None:
    parts = list(validated.worksheet_parts.values())
    if not (
        validated.source_sha256 == sha256(data)
        and validated.size_bytes == len(data) <= limits.max_file_bytes
        and 1 <= len(parts) == len(set(parts))
        # Worksheets only; chartsheets are in worksheet_parts but not in the count.
        and 0 <= validated.worksheet_count <= min(len(parts), limits.max_worksheets)
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
        self,
        data: bytes,
        filename: str,
        principal: Principal,
        session_id: str | None,
        workbook_id: str | None = None,
    ) -> UploadResponse:
        services = self.services
        self.check_session(session_id, principal)
        if principal.role != Role.USER:
            raise ProjectException(403, "FORBIDDEN", "A user identity is required for uploads.")
        existing = (
            self.authorize_workbook(workbook_id, principal, session_id, True)
            if workbook_id is not None
            else None
        )
        repository, blobs, runner = services.repository, services.blob_store, services.runner
        if repository is None or blobs is None or runner is None:
            raise ProjectException(503, "SERVICE_UNAVAILABLE", "Workbook processing unavailable.")
        if (
            not filename.strip()
            or len(filename) > 255
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in filename)
        ):
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
        self._require_ruleset()
        now = datetime.now(UTC)
        wid, vid, aid, jid = (identifier() for _ in range(4))
        if existing is not None:
            wid = existing.workbook_id
        namespace = f"tests/{session_id}" if session_id else "ordinary"
        key = f"{namespace}/{wid}/{vid}/source"
        workbook = existing or WorkbookRecord(
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
        # Durable reservation makes failed/ambiguous writes recoverable after restart.
        # Source keys are server-owned and immutable; the staging lease fences a late acceptance.
        repository.reserve_upload_blob(version, datetime.now(UTC) + timedelta(seconds=30))
        try:
            returned_hash = blobs.put_source(key, data)
            if returned_hash != validated.source_sha256:
                raise IntegrityError("Source write checksum differs")
            retained = blobs.read_source(key)
            if len(retained) != len(data) or sha256(retained) != validated.source_sha256:
                raise IntegrityError("Source read-back checksum differs")
            # Queue time starts at acceptance, after bounded immutable storage I/O.
            accepted_at = datetime.now(UTC)
            version = version.model_copy(update={"created_at": accepted_at})
            audit = audit.model_copy(update={"started_at": accepted_at})
            job = job.model_copy(
                update={
                    "created_at": accepted_at,
                    "queue_deadline": accepted_at
                    + timedelta(seconds=services.settings.queue_timeout_seconds),
                }
            )
            if existing is None:
                workbook = workbook.model_copy(update={"created_at": accepted_at})
            repository.create_upload(workbook, version, audit, job)
        except Exception:
            # A commit can succeed before its acknowledgement is lost. Never roll
            # back bytes until a metadata read proves this version was not committed.
            try:
                committed = repository.get_version(wid, vid)
            except Exception:
                raise StorageError("Upload acceptance could not be confirmed") from None
            if committed is not None:
                if committed.blob_key != key or committed.source_sha256 != validated.source_sha256:
                    raise IntegrityError("Accepted source identity differs") from None
            else:
                try:
                    repository.abandon_upload_blob(vid)
                    blobs.delete_uncommitted_source(key, validated.source_sha256)
                except StorageError:
                    logger.error("source_rollback_failed", extra={"jobId": jid, "auditId": aid})
                raise
        return UploadResponse(workbookId=wid, versionId=vid, auditId=aid, status="PROCESSING")

    def _require_ruleset(self) -> None:
        engine = self.services.engine
        if engine is None or self.services.settings.ruleset_version not in getattr(
            engine, "supported_rulesets", ("1.0.0",)
        ):
            raise ProjectException(
                503,
                "SERVICE_UNAVAILABLE",
                "The installed engine does not support the configured ruleset.",
            )

    def authorize_workbook(
        self, wid: str, principal: Principal, session_id: str | None, write: bool = False
    ) -> WorkbookRecord:
        self.check_session(session_id, principal)
        repository = self.services.repository
        if repository is None:
            raise ProjectException(503, "SERVICE_UNAVAILABLE", "Metadata service unavailable.")
        workbook = repository.get_workbook(wid)
        if workbook is None or workbook.session_id != session_id:
            raise ProjectException(404, "NOT_FOUND", "Workbook not found in this namespace.")
        self._authorize(workbook.owner_subject, workbook.session_id, session_id, principal)
        if write and (principal.role != Role.USER or principal.subject != workbook.owner_subject):
            raise ProjectException(403, "FORBIDDEN", "Only the owner may change this workbook.")
        return workbook

    def authorize_version(
        self, wid: str, vid: str, principal: Principal, session_id: str | None, write: bool = False
    ) -> VersionRecord:
        self.check_session(session_id, principal)
        repository = self.services.repository
        if repository is None:
            raise ProjectException(503, "SERVICE_UNAVAILABLE", "Metadata service unavailable.")
        version = repository.get_version(wid, vid)
        if version is None or version.session_id != session_id:
            raise ProjectException(404, "NOT_FOUND", "Version not found in this namespace.")
        self._authorize(version.owner_subject, version.session_id, session_id, principal)
        if write and (principal.role != Role.USER or principal.subject != version.owner_subject):
            raise ProjectException(403, "FORBIDDEN", "Only the owner may change this workbook.")
        repository.recover_stale_jobs(datetime.now(UTC))
        refreshed = repository.get_version(wid, vid)
        if refreshed is None:
            raise ProjectException(404, "NOT_FOUND", "Version no longer exists.")
        return refreshed

    def authorize_audit(
        self, wid: str, vid: str, aid: str, principal: Principal, session_id: str | None
    ) -> AuditRecord:
        self.check_session(session_id, principal)
        repository = self.services.repository
        if repository is None:
            raise ProjectException(503, "SERVICE_UNAVAILABLE", "Metadata service unavailable.")
        # Resolve every path relationship before ownership, including mismatched audit IDs.
        audit = repository.get_audit(wid, vid, aid)
        if audit is None or audit.session_id != session_id:
            raise ProjectException(404, "NOT_FOUND", "Audit not found in this namespace.")
        self._authorize(audit.owner_subject, audit.session_id, session_id, principal)
        repository.recover_stale_jobs(datetime.now(UTC))
        refreshed = repository.get_audit(wid, vid, aid)
        if refreshed is None:
            raise ProjectException(404, "NOT_FOUND", "Audit no longer exists.")
        return refreshed

    def _version(
        self, wid: str, vid: str, principal: Principal, session_id: str | None
    ) -> VersionRecord:
        return self.authorize_version(wid, vid, principal, session_id)

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
        audit = self.authorize_audit(wid, vid, aid, principal, session_id)
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

    def check_operation(
        self,
        wid: str,
        vid: str,
        principal: Principal,
        session_id: str | None,
        aid: str | None = None,
    ) -> VersionRecord:
        if aid is not None:
            self.authorize_audit(wid, vid, aid, principal, session_id)
        version = self.authorize_version(wid, vid, principal, session_id, True)
        repository = self.services.repository
        assert repository is not None
        if repository.has_active_operation(vid):
            raise ProjectException(409, "ACTIVE_OPERATION", "This version has an active operation.")
        if aid is not None:
            if aid != version.latest_audit_id:
                raise ProjectException(409, "STALE_AUDIT", "Use the latest accepted audit.")
            audit = repository.get_audit(wid, vid, aid)
            assert audit is not None
            if audit.status in (AuditStatus.PROCESSING, AuditStatus.PROCESSING_FAILED):
                raise ProjectException(409, "AUDIT_NOT_READY", "Use a successful completed audit.")
        return version

    def _new_audit(self, version: VersionRecord) -> tuple[AuditRecord, JobRecord]:
        now = datetime.now(UTC)
        aid = identifier()
        audit = AuditRecord(
            audit_id=aid,
            workbook_id=version.workbook_id,
            version_id=version.version_id,
            owner_subject=version.owner_subject,
            session_id=version.session_id,
            ruleset_version=self.services.settings.ruleset_version,
            status=AuditStatus.PROCESSING,
            started_at=now,
            completed_at=None,
            findings=[],
            limitations=[],
            error=None,
        )
        job = JobRecord(
            job_id=identifier(),
            audit_id=aid,
            workbook_id=version.workbook_id,
            version_id=version.version_id,
            session_id=version.session_id,
            state=JobState.QUEUED,
            created_at=now,
            queue_deadline=now + timedelta(seconds=self.services.settings.queue_timeout_seconds),
            worker_id=None,
            lease_expires_at=None,
        )
        return audit, job

    def reaudit(
        self, wid: str, vid: str, principal: Principal, session_id: str | None, body: object
    ) -> ReauditResponse:
        version = self.check_operation(wid, vid, principal, session_id)
        try:
            ReauditRequest.model_validate(body)
        except ValidationError:
            raise ProjectException(
                400, "INVALID_REQUEST", "The re-audit request is invalid."
            ) from None
        self._require_ruleset()
        audit, job = self._new_audit(version)
        assert self.services.repository is not None
        self.services.repository.create_reaudit(version, audit, job)
        return ReauditResponse(
            auditId=audit.audit_id,
            versionId=vid,
            rulesetVersion=audit.ruleset_version,
            status="PROCESSING",
        )

    def get_review(
        self, wid: str, vid: str, aid: str, principal: Principal, session_id: str | None
    ) -> ReviewResponse:
        audit = self.authorize_audit(wid, vid, aid, principal, session_id)
        assert self.services.repository is not None
        version = self.services.repository.get_version(wid, vid)
        assert version is not None
        if version.latest_audit_id != aid:
            raise ProjectException(409, "STALE_AUDIT", "Use the latest accepted audit.")
        if audit.status in (AuditStatus.PROCESSING, AuditStatus.PROCESSING_FAILED):
            raise ProjectException(409, "AUDIT_NOT_READY", "The audit is not ready for review.")
        if audit.review_context is None:
            raise ProjectException(
                409, "LEGACY_REVIEW_UNAVAILABLE", "Request a new audit for stored review context."
            )
        items = audit.review_context
        if principal.role == Role.REVIEWER:
            items = [item.model_copy(update={"allowedActions": []}) for item in items]
        response = ReviewResponse(workbookId=wid, versionId=vid, auditId=aid, items=items)
        bounded_json(response.model_dump(mode="json"))
        return response

    def get_history(
        self, wid: str, principal: Principal, session_id: str | None
    ) -> HistoryResponse:
        self.authorize_workbook(wid, principal, session_id)
        repository = self.services.repository
        assert repository is not None
        repository.recover_stale_jobs(datetime.now(UTC))
        versions, audits = repository.history_records(wid)
        response = HistoryResponse(
            workbookId=wid,
            versions=[
                HistoryVersion(
                    versionId=v.version_id,
                    sourceVersionId=v.source_version_id,
                    status=v.status,
                    createdAt=v.created_at,
                )
                for v in versions
            ],
            audits=[
                HistoryAudit(
                    auditId=a.audit_id,
                    versionId=a.version_id,
                    rulesetVersion=a.ruleset_version,
                    status=a.status,
                )
                for a in audits
            ],
        )
        bounded_json(response.model_dump(mode="json"))
        return response

    def _read_version(self, version: VersionRecord) -> bytes:
        blobs = self.services.blob_store
        if blobs is None:
            raise ProjectException(503, "SERVICE_UNAVAILABLE", "Source storage unavailable.")
        data = blobs.read_source(version.blob_key)
        if len(data) != version.size_bytes or sha256(data) != version.source_sha256:
            raise IntegrityError("Stored version integrity differs")
        return data

    def get_source(
        self, wid: str, vid: str, principal: Principal, session_id: str | None
    ) -> tuple[bytes, str]:
        version = self.authorize_version(wid, vid, principal, session_id)
        return self._read_version(version), version.file_name

    def get_output(
        self, oid: str, principal: Principal, session_id: str | None
    ) -> tuple[bytes, str]:
        self.check_session(session_id, principal)
        repository = self.services.repository
        if repository is None:
            raise ProjectException(503, "SERVICE_UNAVAILABLE", "Metadata unavailable.")
        output = repository.get_output(oid)
        if output is None or output.session_id != session_id:
            raise ProjectException(404, "NOT_FOUND", "Output not found in this namespace.")
        self._authorize(output.owner_subject, output.session_id, session_id, principal)
        repository.recover_stale_jobs(datetime.now(UTC))
        output = repository.get_output(oid)
        if output is None:
            raise ProjectException(404, "NOT_FOUND", "Output no longer exists.")
        if not output.published:
            raise ProjectException(409, "OUTPUT_NOT_READY", "This output is unpublished.")
        version = repository.get_version(output.workbook_id, output.version_id)
        if version is None:
            raise IntegrityError("Published output version missing")
        return self._read_version(version), version.file_name

    def get_report(
        self, wid: str, vid: str, aid: str, principal: Principal, session_id: str | None
    ) -> tuple[bytes, str]:
        audit = self.authorize_audit(wid, vid, aid, principal, session_id)
        if audit.status == AuditStatus.PROCESSING:
            raise ProjectException(409, "REPORT_NOT_READY", "The audit is still processing.")
        assert self.services.repository is not None
        content = self.services.repository.get_report(aid)
        if content is None:
            raise ProjectException(
                409, "LEGACY_REPORT_UNAVAILABLE", "This historical audit has no report snapshot."
            )
        if len(content) > 67_108_864:
            raise ProjectException(
                500, "RESULT_TOO_LARGE", "The audit report exceeds its size limit."
            )
        return content, f"audit-{aid}.json"

    def submit_remediation(
        self,
        wid: str,
        vid: str,
        principal: Principal,
        session_id: str | None,
        key: str | None,
        body: object,
    ) -> RemediationAccepted:
        version = self.authorize_version(wid, vid, principal, session_id, True)
        # Envelope parsing resolves audit identity. Per-finding semantics follow state checks.
        request = parse_remediation_request(body)
        audit = self.authorize_audit(wid, vid, request.auditId, principal, session_id)
        try:
            if key is None:
                raise ValueError("missing")
            TypeAdapter(Identifier).validate_python(key)
        except (ValueError, ValidationError):
            raise ProjectException(
                400, "IDEMPOTENCY_KEY_REQUIRED", "A valid Idempotency-Key is required."
            ) from None
        digest = canonical_request(request)
        path = (
            f"{self.services.settings.api_root_path}/api/v1/workbooks/"
            f"{wid}/versions/{vid}/remediations"
        )
        repository = self.services.repository
        assert repository is not None
        replay = repository.find_idempotency(
            principal.subject, session_id or "ordinary", "POST", path, key, digest
        )
        if replay is not None:
            return replay
        try:
            version = self.check_operation(wid, vid, principal, session_id, request.auditId)
        except ProjectException as exc:
            if exc.status_code == 409:
                # Another identical submission may have committed between our
                # first key read and the operation-state check. Its key and lock
                # become visible atomically, so repeat the lookup before conflict.
                replay = repository.find_idempotency(
                    principal.subject, session_id or "ordinary", "POST", path, key, digest
                )
                if replay is not None:
                    return replay
            raise
        if audit.review_context is None:
            raise ProjectException(
                409,
                "LEGACY_REVIEW_UNAVAILABLE",
                "Request a new audit for stored correction context.",
            )
        if version.validated is None:
            raise IntegrityError("Source validation inventory absent")
        decisions = validate_decisions(request, audit, audit.review_context, version.validated)
        self._require_ruleset()
        if any(decision.action == "APPLY" for decision in decisions):
            engine = require_correction_engine(self.services.engine)
            data = self._read_version(version)
            try:
                measurement = validate_decisions_bounded(
                    engine,
                    data,
                    version.validated,
                    audit,
                    decisions,
                    timeout_seconds=self.services.settings.validation_timeout_seconds,
                    memory_limit_mib=self.services.settings.engine_memory_limit_mib,
                    session_id=session_id,
                    revision=self.services.settings.deployment_sha,
                    measure=self.services.settings.enable_code_measurement,
                )
            except EngineExecutionError as exc:
                record_measurement(self.services, session_id, exc.measurement)
                raise
            record_measurement(self.services, session_id, measurement)
        decisions = sorted(decisions, key=lambda decision: decision.findingId)
        now = datetime.now(UTC)
        rid, jid, ovid, oaid, oid = (identifier() for _ in range(5))
        namespace = f"tests/{session_id}" if session_id else "ordinary"
        remediation = RemediationRecord(
            remediation_id=rid,
            workbook_id=wid,
            source_version_id=vid,
            source_audit_id=audit.audit_id,
            owner_subject=version.owner_subject,
            session_id=version.session_id,
            ruleset_version=self.services.settings.ruleset_version,
            status="REMEDIATING",
            created_at=now,
            decisions=decisions,
            output_version_id=None,
            output_id=None,
            audit_id=None,
            error=None,
        )
        job = RemediationJob(
            job_id=jid,
            remediation_id=rid,
            workbook_id=wid,
            version_id=vid,
            session_id=session_id,
            state=JobState.QUEUED,
            created_at=now,
            queue_deadline=now + timedelta(seconds=self.services.settings.queue_timeout_seconds),
            worker_id=None,
            lease_expires_at=None,
            output_version_id=ovid,
            output_audit_id=oaid,
            output_id=oid,
            blob_key=f"{namespace}/{wid}/{ovid}/source",
        )
        return repository.create_remediation(version, remediation, job, key, digest, path)

    def get_remediation(
        self, rid: str, principal: Principal, session_id: str | None
    ) -> RemediationResponse:
        self.check_session(session_id, principal)
        repository = self.services.repository
        if repository is None:
            raise ProjectException(503, "SERVICE_UNAVAILABLE", "Metadata unavailable.")
        remediation = repository.get_remediation(rid)
        if remediation is None or remediation.session_id != session_id:
            raise ProjectException(404, "NOT_FOUND", "Remediation not found in this namespace.")
        self._authorize(remediation.owner_subject, remediation.session_id, session_id, principal)
        repository.recover_stale_jobs(datetime.now(UTC))
        remediation = repository.get_remediation(rid)
        if remediation is None:
            raise ProjectException(404, "NOT_FOUND", "Remediation no longer exists.")
        return RemediationResponse(
            remediationId=rid,
            status=remediation.status,
            sourceVersionId=remediation.source_version_id,
            outputVersionId=remediation.output_version_id,
            outputId=remediation.output_id,
            auditId=remediation.audit_id,
            error=remediation.error,
        )
