"""Backend decisions use contract errors, typed values, and immutable source context."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.contracts import phase4
from app.contracts.errors import ProjectException
from app.contracts.models import AuditRecord, Finding, ValidatedWorkbook
from app.contracts.phase4 import ReviewItem
from app.services.decisions import parse_remediation_request, validate_decisions


def context(rule: str = "ALT_TEXT") -> tuple[AuditRecord, list[ReviewItem], ValidatedWorkbook]:
    finding = Finding(
        findingId="a" * 64,
        ruleId=rule,
        category={
            "ALT_TEXT": "NON_TEXT_CONTENT",
            "WORKSHEET_NAME": "WORKSHEET_NAME",
            "TABLE_HEADER": "TABLE_STRUCTURE",
            "LINK_TEXT": "LINK_TEXT",
        }[rule],
        worksheet="Sheet1",
        target="xl/worksheets/sheet1.xml#range=A2:B3",
        classification="REQUIRES_USER_INPUT",
        message="Author input required.",
        recommendation="Supply approved content.",
    )
    audit = AuditRecord(
        audit_id="audit",
        workbook_id="workbook",
        version_id="version",
        owner_subject="owner",
        session_id=None,
        ruleset_version="2.0.0",
        status="REMEDIATION_AVAILABLE",
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        findings=[finding],
        limitations=[],
        error=None,
    )
    review = [
        ReviewItem(
            findingId=finding.findingId,
            allowedActions=["APPLY", "DECLINE", "MANUAL_REVIEW"],
            currentValue={"headerRow": 2} if rule == "TABLE_HEADER" else None,
            reason="Approved input may be applied.",
        )
    ]
    validated = ValidatedWorkbook(
        source_sha256="0" * 64,
        size_bytes=1,
        worksheet_count=2,
        nonempty_cell_count=1,
        worksheet_parts={
            "Sheet1": "xl/worksheets/sheet1.xml",
            "Expenses": "xl/worksheets/sheet2.xml",
        },
    )
    return audit, review, validated


def request(value: object = ..., action: str = "APPLY") -> object:
    decision = {"findingId": "a" * 64, "action": action}
    if value is not ...:
        decision["value"] = value
    return {"auditId": "audit", "decisions": [decision]}


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        (request(), 400, "MISSING_VALUE"),
        (request(None), 422, "INVALID_CORRECTION"),
        (request("text"), 422, "INVALID_CORRECTION"),
        (request({"title": "Title"}), 422, "INVALID_CORRECTION"),
        (request({"title": " ", "description": "Description"}), 422, "INVALID_CORRECTION"),
        (request({"title": "x" * 201, "description": "Description"}), 422, "INVALID_CORRECTION"),
        (request({"title": 1, "description": "Description"}), 422, "INVALID_CORRECTION"),
        (request({"title": "Title\x00", "description": "Description"}), 422, "INVALID_CORRECTION"),
        (request(None, "DECLINE"), 400, "INVALID_REQUEST"),
    ],
)
def test_error_distinctions(body: object, status: int, code: str) -> None:
    with pytest.raises(ProjectException) as failure:
        validate_decisions(parse_remediation_request(body), *context())
    assert (failure.value.status_code, failure.value.code) == (status, code)


def test_preserves_exact_validated_user_text() -> None:
    value = {"title": "  Chart\t\n", "description": "Δ & < > _x0041_\r\n"}
    decisions = validate_decisions(parse_remediation_request(request(value)), *context())
    assert decisions[0].value == value


@pytest.mark.parametrize(
    "body",
    [
        {"auditId": "audit", "decisions": []},
        {"auditId": 1, "decisions": [request()["decisions"][0]]},
        {"auditId": "audit", "decisions": [{"findingId": "a" * 64, "action": "UNKNOWN"}]},
        {"auditId": "audit", "decisions": [{"findingId": "a" * 64, "action": "DECLINE", "x": 1}]},
        {"auditId": "audit", "decisions": [{"findingId": "a" * 64, "action": "DECLINE"}], "x": 1},
    ],
)
def test_malformed_envelopes(body: object) -> None:
    with pytest.raises(ProjectException) as failure:
        parse_remediation_request(body)
    assert (failure.value.status_code, failure.value.code) == (400, "INVALID_REQUEST")


def test_decline_has_no_value_and_does_not_mutate_context() -> None:
    audit, review, validated = context()
    before = audit.model_dump()
    decisions = validate_decisions(
        parse_remediation_request(request(action="DECLINE")), audit, review, validated
    )
    assert "value" not in decisions[0].model_dump(exclude_unset=True)
    assert audit.model_dump() == before


def test_unknown_and_duplicate_findings() -> None:
    unknown = {"auditId": "audit", "decisions": [{"findingId": "b" * 64, "action": "DECLINE"}]}
    with pytest.raises(ProjectException) as failure:
        validate_decisions(parse_remediation_request(unknown), *context())
    assert failure.value.code == "FINDING_NOT_FOUND"
    duplicate = {
        "auditId": "audit",
        "decisions": [
            {"findingId": "a" * 64, "action": "DECLINE"},
            {"findingId": "a" * 64, "action": "DECLINE"},
        ],
    }
    with pytest.raises(ProjectException) as failure:
        validate_decisions(parse_remediation_request(duplicate), *context())
    assert failure.value.code == "DUPLICATE_DECISION"


@pytest.mark.parametrize(
    "name,code",
    [
        ("Sheet01", "INVALID_CORRECTION"),
        (" History ", "INVALID_CORRECTION"),
        ("History", "INVALID_CORRECTION"),
        ("sheet1", "UNSUPPORTED_CORRECTION"),
        ("Expenses", "CONFLICTING_CORRECTIONS"),
        ("'Summary", "INVALID_CORRECTION"),
        ("Sum/mary", "INVALID_CORRECTION"),
        ("Summary\x7f", "INVALID_CORRECTION"),
    ],
)
def test_worksheet_name_validation(name: str, code: str) -> None:
    with pytest.raises(ProjectException) as failure:
        validate_decisions(
            parse_remediation_request(request({"name": name})), *context("WORKSHEET_NAME")
        )
    assert failure.value.code == code


@pytest.mark.parametrize("text", ["click\u00a0HERE!", "www.example.edu", "https://example.edu"])
def test_ambiguous_link_replacements(text: str) -> None:
    with pytest.raises(ProjectException) as failure:
        validate_decisions(
            parse_remediation_request(request({"text": text})), *context("LINK_TEXT")
        )
    assert failure.value.code == "INVALID_CORRECTION"


@pytest.mark.parametrize("header", [True, "2", 1, 3])
def test_header_row_requires_exact_integer_candidate(header: object) -> None:
    with pytest.raises(ProjectException) as failure:
        validate_decisions(
            parse_remediation_request(request({"headerRow": header})), *context("TABLE_HEADER")
        )
    assert failure.value.code == "INVALID_CORRECTION"


def test_manual_apply_is_unsupported() -> None:
    audit, review, validated = context()
    audit = audit.model_copy(
        update={
            "findings": [audit.findings[0].model_copy(update={"classification": "MANUAL_REVIEW"})]
        }
    )
    with pytest.raises(ProjectException) as failure:
        validate_decisions(
            parse_remediation_request(request({"title": "Title", "description": "Description"})),
            audit,
            review,
            validated,
        )
    assert failure.value.code == "UNSUPPORTED_CORRECTION"


def test_first_semantic_error_is_reported_in_input_order() -> None:
    body = {
        "auditId": "audit",
        "decisions": [
            {"findingId": "a" * 64, "action": "APPLY"},
            {"findingId": "b" * 64, "action": "DECLINE"},
        ],
    }
    with pytest.raises(ProjectException) as failure:
        validate_decisions(parse_remediation_request(body), *context())
    assert failure.value.code == "MISSING_VALUE"
    body["decisions"].reverse()
    with pytest.raises(ProjectException) as failure:
        validate_decisions(parse_remediation_request(body), *context())
    assert failure.value.code == "FINDING_NOT_FOUND"


def test_no_edit_decision_survives_durable_roundtrip() -> None:
    first = parse_remediation_request(request(action="DECLINE"))
    restored = phase4.RemediationRequest.model_validate_json(first.model_dump_json())
    assert restored == first
    assert "value" not in restored.decisions[0].model_fields_set
    validate_decisions(restored, *context())


def test_companion_wire_examples_and_serializations_conform() -> None:
    root = Path(__file__).resolve().parents[2] / "docs/contracts/phase4"
    schemas = json.loads((root / "phase4-wire-schemas.json").read_text())
    examples = json.loads((root / "phase4-examples.json").read_text())["examples"]
    checked = 0
    for example in examples:
        model = getattr(phase4, example["definition"], None)
        if model is None:
            continue
        validator = Draft202012Validator(schemas | {"$ref": f"#/$defs/{example['definition']}"})
        result = model.model_validate(example["value"])
        validator.validate(result.model_dump(mode="json"))
        Draft202012Validator(model.model_json_schema()).validate(example["value"])
        checked += 1
    assert checked == 10


@pytest.mark.parametrize(
    "status,error,outputs",
    [
        ("REMEDIATING", None, ("version", "output", "audit")),
        ("REMEDIATED", None, (None, None, None)),
        ("PROCESSING_FAILED", None, (None, None, None)),
    ],
)
def test_remediation_state_coherence(
    status: str, error: object, outputs: tuple[object, ...]
) -> None:
    with pytest.raises(ValidationError):
        phase4.RemediationResponse.model_validate(
            {
                "remediationId": "remediation",
                "sourceVersionId": "source",
                "status": status,
                "outputVersionId": outputs[0],
                "outputId": outputs[1],
                "auditId": outputs[2],
                "error": error,
            }
        )
