"""Immutable audit completion snapshots and checks at the engine boundary."""

import hashlib
import json
from datetime import datetime
from typing import Any

from app.contracts.errors import ProjectException
from app.contracts.models import (
    AuditEvaluation,
    AuditRecord,
    AuditStatus,
    FindingClassification,
    ProjectError,
    VersionRecord,
)
from app.contracts.phase4 import RULE_IDS, AuditReport, RemediationRecord, ReportDecision

MAX_JSON_BYTES = 67_108_864


def bounded_json(value: Any) -> bytes:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > MAX_JSON_BYTES:
        raise ProjectException(500, "RESULT_TOO_LARGE", "Serialized result exceeds its size limit.")
    return encoded


def validate_evaluation(evaluation: AuditEvaluation, ruleset_version: str) -> None:
    if len({f.findingId for f in evaluation.findings}) != len(evaluation.findings):
        raise ProjectException(500, "ENGINE_RESULT_INVALID", "Duplicate finding identifiers.")
    if ruleset_version != "2.0.0":
        return
    if evaluation.checks_performed != RULE_IDS or evaluation.review_context is None:
        raise ProjectException(
            500, "ENGINE_RESULT_INVALID", "Six-rule inventory and review context are required."
        )
    findings = sorted(evaluation.findings, key=lambda f: (f.ruleId, f.worksheet, f.target))
    contexts = evaluation.review_context
    if [item.findingId for item in contexts] != [f.findingId for f in findings]:
        raise ProjectException(
            500, "ENGINE_RESULT_INVALID", "Review inventory differs from audit findings."
        )
    categories = dict(
        zip(
            RULE_IDS,
            [
                "NON_TEXT_CONTENT",
                "WORKSHEET_NAME",
                "TABLE_STRUCTURE",
                "MERGED_CELLS",
                "LINK_TEXT",
                "COLOR_OR_CONTRAST",
            ],
            strict=True,
        )
    )
    for finding, item in zip(findings, contexts, strict=True):
        actions = ["DECLINE", "MANUAL_REVIEW"]
        if finding.classification == FindingClassification.REQUIRES_USER_INPUT:
            actions.insert(0, "APPLY")
        if (
            finding.ruleId not in categories
            or finding.category != categories[finding.ruleId]
            or finding.classification == FindingClassification.AUTOMATICALLY_REMEDIABLE
            or item.allowedActions != actions
        ):
            raise ProjectException(
                500, "ENGINE_RESULT_INVALID", "Invalid rule classification or correction actions."
            )
        current = item.currentValue
        expected = {
            "ALT_TEXT": {"title", "description"},
            "WORKSHEET_NAME": {"name"},
            "LINK_TEXT": {"text"},
            "TABLE_HEADER": {"headerRow"},
        }.get(finding.ruleId)
        if current is not None:
            if expected is None or set(current) != expected:
                raise ProjectException(
                    500, "ENGINE_RESULT_INVALID", "Invalid correction context shape."
                )
            if finding.ruleId == "TABLE_HEADER":
                valid = type(current["headerRow"]) is int and 1 <= current["headerRow"] <= 1_048_576
            else:
                valid = all(
                    isinstance(v, str) or (finding.ruleId == "ALT_TEXT" and v is None)
                    for v in current.values()
                )
            if not valid:
                raise ProjectException(
                    500, "ENGINE_RESULT_INVALID", "Invalid correction context values."
                )
        elif finding.classification == FindingClassification.REQUIRES_USER_INPUT:
            raise ProjectException(
                500, "ENGINE_RESULT_INVALID", "Editable finding lacks current values."
            )
        if (
            finding.ruleId in {"MERGED_CELLS", "COLOR_CONTRAST"}
            and finding.classification != FindingClassification.MANUAL_REVIEW
        ):
            raise ProjectException(
                500, "ENGINE_RESULT_INVALID", "This rule requires manual review."
            )
    bounded_json(evaluation.model_dump(mode="json"))


def make_audit_result(
    audit: AuditRecord,
    evaluation: AuditEvaluation | None,
    error: ProjectError | None,
    completed_at: datetime,
) -> AuditRecord:
    if evaluation is not None:
        validate_evaluation(evaluation, audit.ruleset_version)
    findings = (
        sorted(evaluation.findings, key=lambda f: (f.ruleId, f.worksheet, f.target))
        if evaluation
        else []
    )
    if error:
        status = AuditStatus.PROCESSING_FAILED
    elif not findings:
        status = AuditStatus.AUDIT_COMPLETE
    elif audit.ruleset_version == "2.0.0" and any(
        f.classification == FindingClassification.REQUIRES_USER_INPUT for f in findings
    ):
        status = AuditStatus.REMEDIATION_AVAILABLE
    else:
        status = AuditStatus.REVIEW_REQUIRED
    result = AuditRecord.model_validate(
        audit.model_dump()
        | {
            "status": status,
            "completed_at": completed_at,
            "findings": findings,
            "limitations": sorted(set(evaluation.limitations)) if evaluation else [],
            "error": error,
            "review_context": evaluation.review_context if evaluation else None,
            "checks_performed": evaluation.checks_performed if evaluation else [],
        }
    )
    bounded_json(result.model_dump(mode="json"))
    return result


def report_snapshot(
    version: VersionRecord,
    audit: AuditRecord,
    remediation: RemediationRecord | None = None,
    source_audit: AuditRecord | None = None,
) -> bytes:
    assert audit.completed_at is not None
    decisions: list[ReportDecision] = []
    if remediation is not None:
        assert source_audit is not None
        indexed = {decision.findingId: decision for decision in remediation.decisions}
        for finding in source_audit.findings:
            decision = indexed.get(finding.findingId)
            if decision is not None:
                decisions.append(
                    ReportDecision(
                        sourceAuditId=source_audit.audit_id,
                        findingId=finding.findingId,
                        ruleId=finding.ruleId,
                        worksheet=finding.worksheet,
                        target=finding.target,
                        action=decision.action,
                        value=decision.value,
                    )
                )
    report = AuditReport(
        reportVersion="2.0.0",
        specificationVersion="2.1.0",
        workbookId=version.workbook_id,
        versionId=version.version_id,
        auditId=audit.audit_id,
        fileName=version.file_name,
        sha256=version.source_sha256,
        sourceVersionId=version.source_version_id,
        auditDate=audit.completed_at,
        rulesetVersion=audit.ruleset_version,
        status=audit.status,
        checksPerformed=audit.checks_performed or [],
        findings=audit.findings,
        decisions=decisions,
        approvedChanges=[d for d in decisions if d.action == "APPLY"],
        unresolvedFindings=audit.findings,
        manualReviewItems=[
            f for f in audit.findings if f.classification == FindingClassification.MANUAL_REVIEW
        ],
        limitations=audit.limitations,
        remediationId=remediation.remediation_id if remediation else None,
        outputVersionId=remediation.output_version_id if remediation else None,
        outputId=remediation.output_id if remediation else None,
        error=audit.error,
    )
    return bounded_json(report.model_dump(mode="json"))


def canonical_request(request: Any) -> str:
    payload = {
        "auditId": request.auditId,
        "decisions": [
            {"findingId": decision.findingId, "action": decision.action}
            | ({"value": decision.value} if "value" in decision.model_fields_set else {})
            for decision in request.decisions
        ],
    }
    payload["decisions"] = sorted(payload["decisions"], key=lambda decision: decision["findingId"])
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def validate_finding_ids(
    evaluation: AuditEvaluation, source_sha256: str, ruleset_version: str
) -> None:
    """Reject an engine result attributed to different bytes, targets, or ruleset."""
    if ruleset_version != "2.0.0":
        return
    for finding in evaluation.findings:
        identity = [
            source_sha256,
            ruleset_version,
            finding.ruleId,
            finding.worksheet,
            finding.target,
        ]
        expected = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        if finding.findingId != expected:
            raise ProjectException(
                500, "ENGINE_RESULT_INVALID", "Finding identity differs from the evaluated version."
            )
