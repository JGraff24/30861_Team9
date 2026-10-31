"""Shared Team 9 DTOs. Wire names are deliberately explicit, never auto-aliased."""

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictInt,
    StringConstraints,
    model_validator,
)


def nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must contain non-whitespace text")
    return value


Identifier = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
]
SHA256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)]
NonemptyText = Annotated[
    str, StringConstraints(min_length=1, pattern=r"\S"), AfterValidator(nonblank)
]
ReportMessage = Annotated[NonemptyText, StringConstraints(max_length=500)]
FileName = Annotated[NonemptyText, StringConstraints(max_length=255)]
SemVer = Annotated[
    str,
    StringConstraints(
        pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
        r"(-((0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
        r"(\.(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
        r"(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$"
    ),
]
UTCDateTime = Annotated[AwareDatetime, AfterValidator(lambda value: value.astimezone(UTC))]
NonnegativeInt = Annotated[StrictInt, Field(ge=0)]
PositiveInt = Annotated[StrictInt, Field(gt=0)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VersionStatus(StrEnum):
    PROCESSING = "PROCESSING"
    AUDIT_COMPLETE = "AUDIT_COMPLETE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REMEDIATION_AVAILABLE = "REMEDIATION_AVAILABLE"
    REMEDIATING = "REMEDIATING"
    REMEDIATED = "REMEDIATED"
    PROCESSING_FAILED = "PROCESSING_FAILED"


class AuditStatus(StrEnum):
    PROCESSING = "PROCESSING"
    AUDIT_COMPLETE = "AUDIT_COMPLETE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REMEDIATION_AVAILABLE = "REMEDIATION_AVAILABLE"
    PROCESSING_FAILED = "PROCESSING_FAILED"


class FindingCategory(StrEnum):
    NON_TEXT_CONTENT = "NON_TEXT_CONTENT"
    TABLE_STRUCTURE = "TABLE_STRUCTURE"
    MERGED_CELLS = "MERGED_CELLS"
    WORKSHEET_NAME = "WORKSHEET_NAME"
    LINK_TEXT = "LINK_TEXT"
    COLOR_OR_CONTRAST = "COLOR_OR_CONTRAST"


class FindingClassification(StrEnum):
    AUTOMATICALLY_REMEDIABLE = "AUTOMATICALLY_REMEDIABLE"
    REQUIRES_USER_INPUT = "REQUIRES_USER_INPUT"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class Role(StrEnum):
    USER = "user"
    REVIEWER = "reviewer"
    TEST_ADMIN = "test_admin"


class Outcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIP = "skip"


class ErrorSource(StrEnum):
    TESTER = "tester"
    SYSTEM = "system"
    COMMUNICATION = "communication"
    UNKNOWN = "unknown"


class JobState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class SessionState(StrEnum):
    PREPARING = "PREPARING"
    READY = "READY"
    FAILED = "FAILED"
    CLEANED = "CLEANED"


class ExecutionErrorKind(StrEnum):
    VALIDATION_REJECTED = "validation_rejected"
    TIMEOUT = "timeout"
    CRASH = "crash"
    INVALID_RESULT = "invalid_result"


class Principal(ContractModel):
    subject: NonemptyText
    role: Role


class ProjectError(ContractModel):
    code: NonemptyText
    message: NonemptyText
    requestId: Identifier
    details: dict[str, JsonValue]


class TestEndpointError(ContractModel):
    message: NonemptyText
    source: ErrorSource


class HealthResponse(ContractModel):
    status: Literal["AVAILABLE"]
    apiVersion: Literal["1.0"]
    rulesetVersion: SemVer


class UploadResponse(ContractModel):
    workbookId: Identifier
    versionId: Identifier
    auditId: Identifier
    status: Literal["PROCESSING"]


class VersionResponse(ContractModel):
    workbookId: Identifier
    versionId: Identifier
    fileName: FileName
    sha256: SHA256
    createdAt: UTCDateTime
    status: VersionStatus
    latestAuditId: Identifier | None
    sourceVersionId: Identifier | None


class Finding(ContractModel):
    findingId: SHA256
    ruleId: NonemptyText
    category: FindingCategory
    worksheet: str
    target: str
    classification: FindingClassification
    message: ReportMessage
    recommendation: ReportMessage


def check_audit_state(
    status: AuditStatus,
    started: datetime,
    completed: datetime | None,
    findings: list[Finding],
    error: ProjectError | None,
) -> None:
    if completed is not None and completed < started:
        raise ValueError("completion precedes acceptance")
    if status == AuditStatus.PROCESSING:
        if completed is not None or error is not None or findings:
            raise ValueError("processing cannot expose completed results")
    elif status == AuditStatus.PROCESSING_FAILED:
        if completed is None or error is None or findings:
            raise ValueError("failure requires completion/error and no partial findings")
    elif completed is None or error is not None:
        raise ValueError("completed audit requires timestamp and no error")
    if status == AuditStatus.AUDIT_COMPLETE and findings:
        raise ValueError("AUDIT_COMPLETE has no findings")
    if status == AuditStatus.REVIEW_REQUIRED and not findings:
        raise ValueError("REVIEW_REQUIRED requires findings")


class AuditResponse(ContractModel):
    auditId: Identifier
    workbookId: Identifier
    versionId: Identifier
    rulesetVersion: SemVer
    status: AuditStatus
    startedAt: UTCDateTime
    completedAt: UTCDateTime | None
    findings: list[Finding]
    limitations: list[NonemptyText]
    error: ProjectError | None

    @model_validator(mode="after")
    def coherent_state(self) -> Self:
        check_audit_state(self.status, self.startedAt, self.completedAt, self.findings, self.error)
        return self


class TesterSetupRequest(ContractModel):
    target_url: NonemptyText


class SystemSetupRequest(ContractModel):
    session_id: Identifier
    setup: dict[str, JsonValue] = Field(
        default_factory=dict,
        json_schema_extra={
            "properties": {
                "cleanup_session_ids": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 64,
                        "pattern": "^[A-Za-z0-9_-]+$",
                    },
                },
                "faults": {
                    "type": "object",
                    "properties": {"audit_failures": {"type": "integer", "const": 1}},
                    "required": ["audit_failures"],
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
    )

    @model_validator(mode="after")
    def known_setup_keys(self) -> Self:
        if set(self.setup) - {"cleanup_session_ids", "faults"}:
            raise ValueError("unknown setup keys")
        if "cleanup_session_ids" in self.setup:
            from pydantic import TypeAdapter

            TypeAdapter(list[Identifier]).validate_python(self.setup["cleanup_session_ids"])
        if "faults" in self.setup:
            faults = self.setup["faults"]
            if not (
                isinstance(faults, dict)
                and set(faults) == {"audit_failures"}
                and type(faults["audit_failures"]) is int
                and faults["audit_failures"] == 1
            ):
                raise ValueError("faults requires exactly audit_failures integer 1")
        return self


class SetupResponse(ContractModel):
    session_id: Identifier


class RunRequest(ContractModel):
    session_id: Identifier


class TestResult(ContractModel):
    __test__ = False
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"outcome": {"const": "error"}}},
                    "then": {"required": ["source"], "properties": {"source": {"type": "string"}}},
                    "else": {"not": {"required": ["source"]}},
                }
            ]
        }
    )
    test_id: NonemptyText
    outcome: Outcome
    message: NonemptyText
    source: ErrorSource | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def error_source(self) -> Self:
        if (self.outcome == Outcome.ERROR) != (self.source is not None):
            raise ValueError("source is required exactly for error outcomes")
        return self


class CoverageMeasure(ContractModel):
    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {"properties": {"covered": {"type": "null"}, "total": {"type": "null"}}},
                {"properties": {"covered": {"type": "integer"}, "total": {"type": "integer"}}},
            ]
        }
    )
    covered: NonnegativeInt | None
    total: NonnegativeInt | None
    basis: NonemptyText

    @model_validator(mode="after")
    def coherent_counts(self) -> Self:
        if (self.covered is None) != (self.total is None):
            raise ValueError("unavailable coverage requires both counts null")
        if self.covered is not None and self.total is not None and self.covered > self.total:
            raise ValueError("covered exceeds total")
        return self


class SystemCoverage(ContractModel):
    code: CoverageMeasure


class SuiteCoverage(SystemCoverage):
    component: CoverageMeasure
    system: CoverageMeasure


class SystemReport(ContractModel):
    session_id: Identifier
    coverage: SystemCoverage


class SuiteReport(ContractModel):
    session_id: Identifier
    target_url: NonemptyText
    results: list[TestResult]
    coverage: SuiteCoverage

    @model_validator(mode="after")
    def unique_results(self) -> Self:
        ids = [result.test_id for result in self.results]
        if len(ids) != len(set(ids)):
            raise ValueError("each planned test appears once")
        return self


class ProcessingLimits(ContractModel):
    max_file_bytes: PositiveInt = 25_000_000
    max_worksheets: PositiveInt = 50
    max_nonempty_cells: PositiveInt = 250_000
    max_zip_entries: PositiveInt = 10_000
    max_expanded_bytes: PositiveInt = 209_715_200
    max_xml_part_bytes: PositiveInt = 52_428_800


class ValidatedWorkbook(ContractModel):
    source_sha256: SHA256
    size_bytes: NonnegativeInt
    worksheet_count: NonnegativeInt
    nonempty_cell_count: NonnegativeInt
    worksheet_parts: dict[str, NonemptyText]


class ReviewItem(ContractModel):
    findingId: SHA256
    allowedActions: list[Literal["APPLY", "DECLINE", "MANUAL_REVIEW"]] = Field(
        max_length=3, json_schema_extra={"uniqueItems": True}
    )
    currentValue: dict[str, JsonValue] | None = Field(
        json_schema_extra={
            "anyOf": [
                {
                    "oneOf": [
                        {
                            "type": "object",
                            "properties": {
                                "title": {"type": ["string", "null"]},
                                "description": {"type": ["string", "null"]},
                            },
                            "required": ["title", "description"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "headerRow": {"type": "integer", "minimum": 1, "maximum": 1048576}
                            },
                            "required": ["headerRow"],
                            "additionalProperties": False,
                        },
                    ]
                },
                {"type": "null"},
            ]
        }
    )
    reason: ReportMessage

    @model_validator(mode="after")
    def coherent_context(self) -> Self:
        if len(self.allowedActions) != len(set(self.allowedActions)):
            raise ValueError("review actions must be unique")
        current = self.currentValue
        if current is None:
            return self
        keys = set(current)
        valid = (
            (
                keys == {"title", "description"}
                and all(value is None or isinstance(value, str) for value in current.values())
            )
            or (
                keys in ({"name"}, {"text"})
                and all(isinstance(value, str) for value in current.values())
            )
            or (
                keys == {"headerRow"}
                and type(current["headerRow"]) is int
                and 1 <= current["headerRow"] <= 1048576
            )
        )
        if not valid:
            raise ValueError("review current value has an unsupported shape")
        return self


class AuditEvaluation(ContractModel):
    findings: list[Finding]
    limitations: list[NonemptyText]
    # None identifies a legacy producer, never an invented empty Phase 4 inventory.
    review_context: list[ReviewItem] | None = None
    checks_performed: list[str] | None = None


class WorkbookRecord(ContractModel):
    workbook_id: Identifier
    owner_subject: NonemptyText
    session_id: Identifier | None
    created_at: UTCDateTime


class VersionRecord(ContractModel):
    version_id: Identifier
    workbook_id: Identifier
    owner_subject: NonemptyText
    session_id: Identifier | None
    file_name: FileName
    source_sha256: SHA256
    blob_key: NonemptyText
    size_bytes: NonnegativeInt
    worksheet_count: NonnegativeInt
    nonempty_cell_count: NonnegativeInt
    created_at: UTCDateTime
    status: VersionStatus
    latest_audit_id: Identifier | None
    source_version_id: Identifier | None
    validated: ValidatedWorkbook | None = None


class AuditRecord(ContractModel):
    audit_id: Identifier
    workbook_id: Identifier
    version_id: Identifier
    owner_subject: NonemptyText
    session_id: Identifier | None
    ruleset_version: SemVer
    status: AuditStatus
    started_at: UTCDateTime
    completed_at: UTCDateTime | None
    findings: list[Finding]
    limitations: list[NonemptyText]
    error: ProjectError | None
    review_context: list[ReviewItem] | None = None
    checks_performed: list[str] | None = None

    @model_validator(mode="after")
    def coherent_state(self) -> Self:
        check_audit_state(
            self.status, self.started_at, self.completed_at, self.findings, self.error
        )
        return self


class JobRecord(ContractModel):
    job_id: Identifier
    audit_id: Identifier
    workbook_id: Identifier
    version_id: Identifier
    session_id: Identifier | None
    state: JobState
    created_at: UTCDateTime
    queue_deadline: UTCDateTime
    worker_id: NonemptyText | None
    lease_expires_at: UTCDateTime | None
    test_audit_failure: bool = False


class TestSession(ContractModel):
    __test__ = False
    session_id: Identifier
    owner_subject: NonemptyText
    allowed_subjects: list[NonemptyText]
    state: SessionState
    created_at: UTCDateTime
    revision: NonemptyText
    cleanup_error: NonemptyText | None
    audit_failures_remaining: Annotated[StrictInt, Field(ge=0, le=1)] = 0


class ExecutionMeasurement(ContractModel):
    session_id: Identifier
    invocation_id: Identifier
    revision: NonemptyText
    executable_lines: dict[str, list[PositiveInt]]
    covered_lines: dict[str, list[PositiveInt]]
    unavailable_reason: NonemptyText | None

    @model_validator(mode="after")
    def bounded_line_sets(self) -> Self:
        for line_map in (self.executable_lines, self.covered_lines):
            for filename, lines in line_map.items():
                path = PurePosixPath(filename)
                if not filename or path.is_absolute() or ".." in path.parts or "\\" in filename:
                    raise ValueError("measurement paths must be relative POSIX source paths")
                if str(path) != filename or ":" in filename or filename == ".":
                    raise ValueError("measurement paths must be canonical relative paths")
                if lines != sorted(set(lines)):
                    raise ValueError("line numbers must be sorted and unique")
        for filename, lines in self.covered_lines.items():
            if not set(lines) <= set(self.executable_lines.get(filename, [])):
                raise ValueError("covered lines must be executable")
        return self


class ExecutionResult(ContractModel):
    operation: Literal["validate", "audit"]
    validated: ValidatedWorkbook | None
    evaluation: AuditEvaluation | None
    measurement: ExecutionMeasurement | None

    @model_validator(mode="after")
    def appropriate_result(self) -> Self:
        if self.operation == "validate":
            if self.validated is None or self.evaluation is not None:
                raise ValueError("validate returns only validated")
        elif self.evaluation is None or self.validated is not None:
            raise ValueError("audit returns only evaluation")
        return self
