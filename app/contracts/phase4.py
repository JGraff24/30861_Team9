"""Phase 4 wire DTOs and durable backend records.

Decision envelopes deliberately accept a JSON value before rule-specific semantic
validation, so malformed requests (400) remain distinct from correction errors (422).
"""

from typing import Annotated, Any, Literal, Self

from pydantic import ConfigDict, Field, JsonValue, StrictInt, StringConstraints, model_validator

from app.contracts.models import (
    SHA256,
    AuditStatus,
    ContractModel,
    FileName,
    Finding,
    Identifier,
    JobState,
    NonemptyText,
    ProjectError,
    ReviewItem,
    SemVer,
    UTCDateTime,
    VersionStatus,
)

__all__ = [
    "AuditReport",
    "Decision",
    "HistoryAudit",
    "HistoryResponse",
    "HistoryVersion",
    "OutputRecord",
    "ReauditRequest",
    "ReauditResponse",
    "RemediationAccepted",
    "RemediationJob",
    "RemediationRecord",
    "RemediationRequest",
    "RemediationResponse",
    "ReportDecision",
    "ReviewItem",
    "ReviewResponse",
]

RULE_IDS = [
    "ALT_TEXT",
    "WORKSHEET_NAME",
    "TABLE_HEADER",
    "MERGED_CELLS",
    "LINK_TEXT",
    "COLOR_CONTRAST",
]
RuleId = Literal[
    "ALT_TEXT", "WORKSHEET_NAME", "TABLE_HEADER", "MERGED_CELLS", "LINK_TEXT", "COLOR_CONTRAST"
]
Action = Literal["APPLY", "DECLINE", "MANUAL_REVIEW"]
RemediationStatus = Literal["REMEDIATING", "REMEDIATED", "PROCESSING_FAILED"]
TerminalAuditStatus = Literal[
    "AUDIT_COMPLETE", "REVIEW_REQUIRED", "REMEDIATION_AVAILABLE", "PROCESSING_FAILED"
]
StrictIdentifier = Annotated[Identifier, StringConstraints(strict=True)]
Text = Annotated[str, StringConstraints(strict=True, pattern=r"\S")]


class AltTextValue(ContractModel):
    title: Annotated[Text, StringConstraints(min_length=1, max_length=200)]
    description: Annotated[Text, StringConstraints(min_length=1, max_length=800)]


class WorksheetNameValue(ContractModel):
    name: Annotated[Text, StringConstraints(min_length=1, max_length=31)]


class LinkTextValue(ContractModel):
    text: Annotated[Text, StringConstraints(min_length=1, max_length=1000)]


class HeaderRowValue(ContractModel):
    headerRow: Annotated[StrictInt, Field(ge=1, le=1_048_576)]


def correction_schema() -> dict[str, Any]:
    """Publish typed values while retaining rule-aware 400/422 runtime handling."""
    return {
        "oneOf": [
            model.model_json_schema()
            for model in (AltTextValue, WorksheetNameValue, LinkTextValue, HeaderRowValue)
        ]
    }


def decision_schema(schema: dict[str, Any]) -> None:
    schema["properties"]["value"] = correction_schema()
    schema["allOf"] = [
        {
            "if": {"properties": {"action": {"const": "APPLY"}}},
            "then": {"required": ["value"]},
            "else": {"not": {"required": ["value"]}},
        }
    ]


def report_decision_schema(schema: dict[str, Any]) -> None:
    schema["properties"]["value"] = {"anyOf": [correction_schema(), {"type": "null"}]}
    schema["allOf"] = [
        {
            "if": {"properties": {"action": {"const": "APPLY"}}},
            "then": {"properties": {"value": correction_schema()}},
            "else": {"properties": {"value": {"type": "null"}}},
        }
    ]


def remediation_response_schema(schema: dict[str, Any]) -> None:
    error = schema["properties"]["error"]["anyOf"][0]
    output_fields = ("outputVersionId", "outputId", "auditId")
    schema["allOf"] = [
        {
            "if": {"properties": {"status": {"const": "REMEDIATING"}}},
            "then": {
                "properties": {
                    **{field: {"type": "null"} for field in output_fields},
                    "error": {"type": "null"},
                }
            },
        },
        {
            "if": {"properties": {"status": {"const": "REMEDIATED"}}},
            "then": {
                "properties": {
                    **{field: {"type": "string"} for field in output_fields},
                    "error": {"type": "null"},
                }
            },
        },
        {
            "if": {"properties": {"status": {"const": "PROCESSING_FAILED"}}},
            "then": {
                "properties": {
                    **{field: {"type": "null"} for field in output_fields},
                    "error": error,
                }
            },
        },
    ]


def audit_report_schema(schema: dict[str, Any]) -> None:
    error = schema["properties"]["error"]["anyOf"][0]
    changes = schema["properties"]["approvedChanges"]
    changes["items"] = {"allOf": [changes["items"], {"properties": {"action": {"const": "APPLY"}}}]}
    schema["properties"]["checksPerformed"]["uniqueItems"] = True
    schema["properties"]["limitations"]["uniqueItems"] = True
    schema["allOf"] = [
        {
            "if": {"properties": {"status": {"const": "PROCESSING_FAILED"}}},
            "then": {
                "properties": {
                    "error": error,
                    "findings": {"maxItems": 0},
                    "unresolvedFindings": {"maxItems": 0},
                    "manualReviewItems": {"maxItems": 0},
                }
            },
            "else": {
                "properties": {
                    "error": {"type": "null"},
                    "checksPerformed": {"const": RULE_IDS},
                }
            },
        },
        {
            "if": {"properties": {"status": {"const": "AUDIT_COMPLETE"}}},
            "then": {"properties": {"findings": {"maxItems": 0}}},
        },
        {
            "if": {
                "properties": {"status": {"enum": ["REVIEW_REQUIRED", "REMEDIATION_AVAILABLE"]}}
            },
            "then": {"properties": {"findings": {"minItems": 1}}},
        },
    ]


class ReauditRequest(ContractModel):
    reason: Literal["MANUAL_REAUDIT"] = "MANUAL_REAUDIT"


class ReauditResponse(ContractModel):
    auditId: Identifier
    versionId: Identifier
    rulesetVersion: SemVer
    status: Literal["PROCESSING"]


class Decision(ContractModel):
    model_config = ConfigDict(json_schema_extra=decision_schema)
    findingId: SHA256
    action: Action
    value: JsonValue = Field(default=None, exclude_if=lambda value: value is None)


class RemediationRequest(ContractModel):
    auditId: StrictIdentifier
    decisions: Annotated[list[Decision], Field(min_length=1, max_length=1000)]


class RemediationAccepted(ContractModel):
    remediationId: Identifier
    sourceVersionId: Identifier
    status: Literal["REMEDIATING"]


class RemediationResponse(ContractModel):
    model_config = ConfigDict(json_schema_extra=remediation_response_schema)
    remediationId: Identifier
    status: RemediationStatus
    sourceVersionId: Identifier
    outputVersionId: Identifier | None
    outputId: Identifier | None
    auditId: Identifier | None
    error: ProjectError | None

    @model_validator(mode="after")
    def coherent_state(self) -> Self:
        outputs = (self.outputVersionId, self.outputId, self.auditId)
        if self.status == "REMEDIATED":
            if any(value is None for value in outputs) or self.error is not None:
                raise ValueError("completed remediation requires output identities and no error")
        elif any(value is not None for value in outputs):
            raise ValueError("unpublished remediation cannot expose output identities")
        elif (self.status == "PROCESSING_FAILED") != (self.error is not None):
            raise ValueError("failed remediation requires error; processing cannot have error")
        return self


class ReviewResponse(ContractModel):
    workbookId: Identifier
    versionId: Identifier
    auditId: Identifier
    items: list[ReviewItem]


class HistoryVersion(ContractModel):
    versionId: Identifier
    sourceVersionId: Identifier | None
    status: VersionStatus
    createdAt: UTCDateTime


class HistoryAudit(ContractModel):
    auditId: Identifier
    versionId: Identifier
    rulesetVersion: SemVer
    status: AuditStatus


class HistoryResponse(ContractModel):
    workbookId: Identifier
    versions: list[HistoryVersion]
    audits: list[HistoryAudit]


class ReportDecision(ContractModel):
    model_config = ConfigDict(json_schema_extra=report_decision_schema)
    sourceAuditId: Identifier
    findingId: SHA256
    ruleId: RuleId
    worksheet: str
    target: NonemptyText
    action: Action
    value: JsonValue

    @model_validator(mode="after")
    def coherent_value(self) -> Self:
        if self.action != "APPLY" and self.value is not None:
            raise ValueError("recorded nonediting decisions require null value")
        if self.action == "APPLY" and not isinstance(self.value, dict):
            raise ValueError("recorded APPLY requires a typed value object")
        return self


class AuditReport(ContractModel):
    model_config = ConfigDict(json_schema_extra=audit_report_schema)
    reportVersion: Literal["2.0.0"]
    specificationVersion: Literal["2.1.0"]
    workbookId: Identifier
    versionId: Identifier
    auditId: Identifier
    fileName: FileName
    sha256: SHA256
    sourceVersionId: Identifier | None
    auditDate: UTCDateTime
    rulesetVersion: SemVer
    status: TerminalAuditStatus
    checksPerformed: list[RuleId]
    findings: list[Finding]
    decisions: list[ReportDecision]
    approvedChanges: list[ReportDecision]
    unresolvedFindings: list[Finding]
    manualReviewItems: list[Finding]
    limitations: list[NonemptyText]
    remediationId: Identifier | None
    outputVersionId: Identifier | None
    outputId: Identifier | None
    error: ProjectError | None

    @model_validator(mode="after")
    def coherent_report(self) -> Self:
        if self.status == "PROCESSING_FAILED":
            if (
                self.error is None
                or self.findings
                or self.unresolvedFindings
                or self.manualReviewItems
            ):
                raise ValueError("failed report requires error and no partial findings")
        elif self.error is not None or self.checksPerformed != RULE_IDS:
            raise ValueError("successful Phase 4 report requires all six checks and no error")
        if (self.status == "AUDIT_COMPLETE" and self.findings) or (
            self.status in {"REVIEW_REQUIRED", "REMEDIATION_AVAILABLE"} and not self.findings
        ):
            raise ValueError("findings must agree with the audit completion status")
        if self.unresolvedFindings != self.findings:
            raise ValueError("unresolved findings must contain exactly the evaluated findings")
        if self.manualReviewItems != [
            item for item in self.findings if item.classification == "MANUAL_REVIEW"
        ]:
            raise ValueError("manual items must project evaluated manual findings")
        if any(item.action != "APPLY" for item in self.approvedChanges):
            raise ValueError("committed changes contain only APPLY decisions")
        if len(self.checksPerformed) != len(set(self.checksPerformed)):
            raise ValueError("checks cannot repeat")
        return self


class RemediationRecord(ContractModel):
    remediation_id: Identifier
    workbook_id: Identifier
    source_version_id: Identifier
    source_audit_id: Identifier
    owner_subject: NonemptyText
    session_id: Identifier | None
    ruleset_version: SemVer = "2.0.0"
    status: RemediationStatus
    created_at: UTCDateTime
    decisions: list[Decision]
    output_version_id: Identifier | None
    output_id: Identifier | None
    audit_id: Identifier | None
    error: ProjectError | None


class RemediationJob(ContractModel):
    job_id: Identifier
    remediation_id: Identifier
    workbook_id: Identifier
    version_id: Identifier
    session_id: Identifier | None
    state: JobState
    created_at: UTCDateTime
    queue_deadline: UTCDateTime
    worker_id: NonemptyText | None
    lease_expires_at: UTCDateTime | None
    output_version_id: Identifier
    output_audit_id: Identifier
    output_id: Identifier
    blob_key: NonemptyText


class OutputRecord(ContractModel):
    output_id: Identifier
    workbook_id: Identifier
    version_id: Identifier
    remediation_id: Identifier
    owner_subject: NonemptyText
    session_id: Identifier | None
    published: bool
