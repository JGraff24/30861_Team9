"""Fail closed at the audit inventory/report boundary without truncating results."""

import hashlib
import json

import pytest

from app.contracts.errors import ProjectException
from app.contracts.models import AuditEvaluation, Finding, ReviewItem
from app.contracts.phase4 import RULE_IDS
from app.services import reports


def evaluation() -> AuditEvaluation:
    identity = ["a" * 64, "2.0.0", "WORKSHEET_NAME", "Sheet1", "sheet.xml#sheetId=1"]
    finding = Finding(
        findingId=hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest(),
        ruleId="WORKSHEET_NAME",
        category="WORKSHEET_NAME",
        worksheet="Sheet1",
        target="sheet.xml#sheetId=1",
        classification="REQUIRES_USER_INPUT",
        message="A generic name needs author input.",
        recommendation="Choose a descriptive worksheet name.",
    )
    return AuditEvaluation(
        findings=[finding],
        limitations=[],
        checks_performed=RULE_IDS,
        review_context=[
            ReviewItem(
                findingId=finding.findingId,
                allowedActions=["APPLY", "DECLINE", "MANUAL_REVIEW"],
                currentValue={"name": "Sheet1"},
                reason="Supported by the safety assessment.",
            )
        ],
    )


def test_current_inventory_is_required_and_identity_belongs_to_source() -> None:
    result = evaluation()
    reports.validate_evaluation(result, "2.0.0")
    reports.validate_finding_ids(result, "a" * 64, "2.0.0")
    with pytest.raises(ProjectException, match="Finding identity"):
        reports.validate_finding_ids(result, "b" * 64, "2.0.0")
    legacy = AuditEvaluation(findings=[], limitations=[])
    reports.validate_evaluation(legacy, "1.0.0")
    with pytest.raises(ProjectException) as failure:
        reports.validate_evaluation(legacy, "2.0.0")
    assert failure.value.code == "ENGINE_RESULT_INVALID"


@pytest.mark.parametrize("defect", ["partial", "context", "actions", "category", "automatic"])
def test_invalid_engine_results_never_become_successful_audits(defect: str) -> None:
    result = evaluation()
    if defect == "partial":
        result = result.model_copy(update={"checks_performed": RULE_IDS[:2]})
    elif defect == "context":
        result = result.model_copy(update={"review_context": []})
    elif defect == "actions":
        context = result.review_context[0].model_copy(update={"allowedActions": ["APPLY"]})
        result = result.model_copy(update={"review_context": [context]})
    else:
        update = (
            {"category": "TABLE_STRUCTURE"}
            if defect == "category"
            else {"classification": "AUTOMATICALLY_REMEDIABLE"}
        )
        result = result.model_copy(
            update={"findings": [result.findings[0].model_copy(update=update)]}
        )
    with pytest.raises(ProjectException) as failure:
        reports.validate_evaluation(result, "2.0.0")
    assert failure.value.code == "ENGINE_RESULT_INVALID"


def test_oversized_json_is_a_controlled_failure_not_partial_content(monkeypatch) -> None:
    monkeypatch.setattr(reports, "MAX_JSON_BYTES", 64)
    with pytest.raises(ProjectException) as failure:
        reports.bounded_json({"findings": ["complete result" * 20]})
    assert (failure.value.status_code, failure.value.code) == (500, "RESULT_TOO_LARGE")
