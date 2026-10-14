"""Owner boundaries. These declarations provide no service implementations."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from app.config import SystemSettings
from app.contracts.models import (
    AuditEvaluation,
    AuditRecord,
    CoverageMeasure,
    ExecutionMeasurement,
    ExecutionResult,
    JobRecord,
    Principal,
    ProcessingLimits,
    ProjectError,
    TestSession,
    ValidatedWorkbook,
    VersionRecord,
    WorkbookRecord,
)


class WorkbookEngine(Protocol):
    def validate(
        self, data: bytes, filename: str, limits: ProcessingLimits
    ) -> ValidatedWorkbook: ...
    def audit(
        self, data: bytes, validated: ValidatedWorkbook, ruleset_version: str
    ) -> AuditEvaluation: ...


class BlobStore(Protocol):
    def health(self) -> bool: ...
    def put_source(self, key: str, data: bytes) -> str: ...
    def read_source(self, key: str) -> bytes: ...
    def delete_uncommitted_source(self, key: str, expected_sha256: str) -> None: ...
    def delete_test_source(self, key: str, session_id: str) -> None: ...


class Repository(Protocol):
    """Mutations are atomic; external blob work stays outside DB transactions.

    create_upload checks READY/allowed subjects within its transaction.
    Claims and terminal transitions enforce worker leases. Cleanup reserves a
    CLEANED tombstone before deleting blobs and never selects ordinary data.
    See docs/contracts/phase3.md for complete transaction requirements.
    """

    def initialize(self) -> None: ...
    def health(self) -> bool: ...
    def create_upload(
        self, workbook: WorkbookRecord, version: VersionRecord, audit: AuditRecord, job: JobRecord
    ) -> None: ...
    def get_version(self, workbook_id: str, version_id: str) -> VersionRecord | None: ...
    def get_audit(self, workbook_id: str, version_id: str, audit_id: str) -> AuditRecord | None: ...
    def get_job(self, job_id: str) -> JobRecord | None: ...
    def claim_next_job(
        self, worker_id: str, now: datetime, lease_seconds: int
    ) -> JobRecord | None: ...
    def complete_audit(
        self, job_id: str, worker_id: str, evaluation: AuditEvaluation, completed_at: datetime
    ) -> None: ...
    def fail_audit(
        self, job_id: str, worker_id: str | None, error: ProjectError, completed_at: datetime
    ) -> None: ...
    def recover_stale_jobs(self, now: datetime) -> int: ...
    def create_test_session(self, session: TestSession) -> None: ...
    def get_test_session(self, session_id: str) -> TestSession | None: ...
    def mark_session_ready(self, session_id: str) -> None: ...
    def mark_session_failed(self, session_id: str, reason: str) -> None: ...
    def begin_session_cleanup(self, session_id: str, owner_subject: str) -> list[str]: ...
    def finish_session_cleanup(self, session_id: str) -> None: ...
    def record_measurement(self, measurement: ExecutionMeasurement) -> None: ...
    def summarize_measurements(self, session_id: str) -> CoverageMeasure: ...


class EngineRunner(Protocol):
    def execute(
        self,
        operation: Literal["validate", "audit"],
        data: bytes,
        filename: str,
        limits: ProcessingLimits,
        ruleset_version: str,
        validated: ValidatedWorkbook | None,
        session_id: str | None,
        revision: str,
        timeout_seconds: float,
        memory_limit_mib: int,
    ) -> ExecutionResult: ...


class SessionService(Protocol):
    def require_ready(self, session_id: str, principal: Principal) -> TestSession: ...
    def prepare(
        self, session_id: str, principal: Principal, setup: dict[str, object]
    ) -> TestSession: ...
    def get_code_coverage(self, session_id: str, principal: Principal) -> CoverageMeasure: ...


class CodeMeasurementStore(Protocol):
    def record_execution(self, session_id: str, measurement: ExecutionMeasurement) -> None: ...
    def summarize(self, session_id: str) -> CoverageMeasure: ...


class AuthService(Protocol):
    # Chosen internal argument: a raw bearer token extracted by the future router.
    def require_user(self, token: str | None) -> Principal: ...
    def require_test_admin(self, token: str | None) -> Principal: ...
    def can_read(self, owner_subject: str, principal: Principal) -> bool: ...


@dataclass(frozen=True, repr=False)
class SystemServices:
    settings: SystemSettings
    repository: Repository | None = None
    blob_store: BlobStore | None = None
    runner: EngineRunner | None = None
    session_service: SessionService | None = None
    auth_service: AuthService | None = None
    engine: WorkbookEngine | None = None

    @property
    def missing_integrations(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in (
                "repository",
                "blob_store",
                "runner",
                "session_service",
                "auth_service",
                "engine",
            )
            if getattr(self, name) is None
        )
