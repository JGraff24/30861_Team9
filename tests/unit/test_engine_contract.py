"""Dylan: engine conforms to the shared Protocol, is deterministic and never mutates input."""

import hashlib
import inspect
import json
import re

import pytest
from pydantic import ValidationError

from app.audit.engine import AuditInputError, OoxmlWorkbookEngine
from app.contracts.models import AuditEvaluation, Finding, ProcessingLimits, ValidatedWorkbook
from app.contracts.ports import WorkbookEngine
from tests.unit.ooxml_builder import (
    Drawing,
    Sheet,
    Workbook,
    anchor,
    chart,
    cnvpr,
    drawing_xml,
    group,
    pic,
    shape,
)

ENGINE = OoxmlWorkbookEngine()


def mixed_workbook(first_name: str = "Picture 1") -> bytes:
    d1 = drawing_xml(
        anchor(pic(cnvpr("2", first_name))),
        anchor(chart(cnvpr("3", "Chart 1", title="Sales"))),
        anchor(
            group(
                cnvpr("4", "Group 1", title="g", descr="g"),
                shape(cnvpr("5", "Rect", decorative="1")),
            )
        ),
        anchor(pic(cnvpr("9", "Dup A"))),
        anchor(pic(cnvpr("9", "Dup B"))),
    )
    d2 = drawing_xml(anchor(shape(cnvpr("2", "Ünïcødé ☃"))))
    return Workbook(
        [
            Sheet(
                "Sheet1",
                "xl/worksheets/sheet1.xml",
                "1",
                drawing=Drawing("xl/drawings/drawing1.xml", d1),
            ),
            Sheet(
                "Données",
                "xl/worksheets/sheet2.xml",
                "2",
                state="hidden",
                drawing=Drawing("xl/drawings/drawing2.xml", d2),
            ),
            Sheet("sheet03", "xl/worksheets/sheet3.xml", "3"),
        ]
    ).build()


def test_engine_matches_shared_protocol_signatures() -> None:
    engine: WorkbookEngine = ENGINE  # static check by mypy-compatible assignment
    for name in ("validate", "audit"):
        expected = inspect.signature(getattr(WorkbookEngine, name))
        actual = inspect.signature(getattr(OoxmlWorkbookEngine, name))
        assert list(actual.parameters) == list(expected.parameters)
    assert engine is ENGINE


def test_returns_frozen_shared_dtos() -> None:
    data = mixed_workbook()
    validated = ENGINE.validate(data, "m.xlsx", ProcessingLimits())
    evaluation = ENGINE.audit(data, validated, "1.0.0")
    assert type(validated) is ValidatedWorkbook and type(evaluation) is AuditEvaluation
    assert all(type(f) is Finding for f in evaluation.findings)
    with pytest.raises(ValidationError):
        validated.size_bytes = 1  # type: ignore[misc]


def test_five_audits_produce_identical_complete_payloads_and_bytes_unchanged() -> None:
    data = mixed_workbook()
    original = hashlib.sha256(data).hexdigest()
    payloads = []
    for _ in range(5):
        engine = OoxmlWorkbookEngine()  # fresh instance each time
        validated = engine.validate(bytes(data), "m.xlsx", ProcessingLimits())
        payloads.append(engine.audit(bytes(data), validated, "1.0.0").model_dump_json())
    assert len(set(payloads)) == 1
    assert hashlib.sha256(data).hexdigest() == original


def test_findings_sorted_and_ids_follow_contract() -> None:
    data = mixed_workbook()
    evaluation = ENGINE.audit(data, ENGINE.validate(data, "m.xlsx", ProcessingLimits()), "1.0.0")
    keys = [(f.ruleId, f.worksheet, f.target) for f in evaluation.findings]
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys)
    source = hashlib.sha256(data).hexdigest()
    for finding in evaluation.findings:
        payload = [source, "1.0.0", finding.ruleId, finding.worksheet, finding.target]
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        assert finding.findingId == hashlib.sha256(encoded).hexdigest()


def test_expected_findings_for_mixed_workbook() -> None:
    """Expectations authored from the seeds in mixed_workbook(), not from audit output."""
    data = mixed_workbook()
    evaluation = ENGINE.audit(data, ENGINE.validate(data, "m.xlsx", ProcessingLimits()), "1.0.0")
    d1 = "xl/drawings/drawing1.xml"
    assert [
        (f.ruleId, f.worksheet, f.target, f.classification.value) for f in evaluation.findings
    ] == [
        ("ALT_TEXT", "Données", "xl/drawings/drawing2.xml#id=2", "REQUIRES_USER_INPUT"),
        ("ALT_TEXT", "Sheet1", f"{d1}#id=2", "REQUIRES_USER_INPUT"),
        ("ALT_TEXT", "Sheet1", f"{d1}#id=3", "REQUIRES_USER_INPUT"),
        ("ALT_TEXT", "Sheet1", f"{d1}#id=5", "MANUAL_REVIEW"),
        # Duplicate ids: MANUAL_REVIEW at the object's indexed path (pod contract 1.1 §7).
        ("ALT_TEXT", "Sheet1", f"{d1}#xml=/wsDr[1]/twoCellAnchor[4]/pic[1]", "MANUAL_REVIEW"),
        ("ALT_TEXT", "Sheet1", f"{d1}#xml=/wsDr[1]/twoCellAnchor[5]/pic[1]", "MANUAL_REVIEW"),
        ("WORKSHEET_NAME", "Sheet1", "xl/worksheets/sheet1.xml#sheetId=1", "REQUIRES_USER_INPUT"),
        ("WORKSHEET_NAME", "sheet03", "xl/worksheets/sheet3.xml#sheetId=3", "REQUIRES_USER_INPUT"),
    ]


def test_ids_independent_of_filename_and_change_with_source_bytes() -> None:
    data = mixed_workbook()
    a = ENGINE.audit(data, ENGINE.validate(data, "team9-upload.xlsx", ProcessingLimits()), "1.0.0")
    b = ENGINE.audit(data, ENGINE.validate(data, "TEAM12 copy.xlsx", ProcessingLimits()), "1.0.0")
    assert a == b
    changed = mixed_workbook("Picture X")
    assert changed != data
    c = ENGINE.audit(changed, ENGINE.validate(changed, "x.xlsx", ProcessingLimits()), "1.0.0")
    assert {f.findingId for f in a.findings}.isdisjoint({f.findingId for f in c.findings})
    d = ENGINE.audit(data, ENGINE.validate(data, "x.xlsx", ProcessingLimits()), "1.1.0")
    assert {f.findingId for f in a.findings}.isdisjoint({f.findingId for f in d.findings})


def test_clean_workbook_has_no_findings_or_limitations() -> None:
    d1 = drawing_xml(anchor(pic(cnvpr("2", "Logo", title="Logo", descr="Company logo"))))
    data = Workbook(
        [
            Sheet(
                "Budget",
                "xl/worksheets/sheet1.xml",
                "1",
                drawing=Drawing("xl/drawings/drawing1.xml", d1),
            )
        ]
    ).build()
    evaluation = ENGINE.audit(data, ENGINE.validate(data, "c.xlsx", ProcessingLimits()), "1.0.0")
    assert evaluation == AuditEvaluation(findings=[], limitations=[])


def test_audit_rejects_bytes_not_matching_validation() -> None:
    data = mixed_workbook()
    validated = ENGINE.validate(data, "m.xlsx", ProcessingLimits())
    other = Workbook([Sheet("Budget", "xl/worksheets/sheet1.xml", "1")]).build()
    with pytest.raises(AuditInputError) as caught:
        ENGINE.audit(other, validated, "1.0.0")
    assert (caught.value.status_code, caught.value.code) == (500, "SOURCE_INTEGRITY_FAILED")


def test_audit_rejects_mismatched_worksheet_map() -> None:
    data = mixed_workbook()
    validated = ENGINE.validate(data, "m.xlsx", ProcessingLimits())
    tampered = validated.model_copy(
        update={"worksheet_parts": {"Sheet1": "xl/worksheets/sheet1.xml"}}
    )
    with pytest.raises(AuditInputError) as caught:
        ENGINE.audit(data, tampered, "1.0.0")
    assert caught.value.code == "AUDIT_INPUT_MISMATCH"


@pytest.mark.parametrize("version", ["1.0", "v1.0.0", "", "01.0.0"])
def test_invalid_ruleset_version_is_rejected(version: str) -> None:
    data = mixed_workbook()
    validated = ENGINE.validate(data, "m.xlsx", ProcessingLimits())
    with pytest.raises(ValueError):
        ENGINE.audit(data, validated, version)


def test_engine_modules_have_no_io_side_effect_imports() -> None:
    import app.audit.engine as engine_module
    import app.audit.rules as rules_module
    import app.workbooks.drawings as drawings_module
    import app.workbooks.package as package_module
    import app.workbooks.structure as structure_module
    import app.workbooks.validation as validation_module

    forbidden = (
        "fastapi",
        "httpx",
        "boto3",
        "sqlite3",
        "socket",
        "openpyxl",
        "subprocess",
        "app.storage",
        "app.api",
        "app.jobs",
        "app.test_interface",
        "logging",
    )
    for module in (
        engine_module,
        rules_module,
        drawings_module,
        package_module,
        structure_module,
        validation_module,
    ):
        source = inspect.getsource(module)
        for name in forbidden:
            assert f"import {name}" not in source and f"from {name}" not in source, (module, name)
        assert ".save(" not in source
        assert re.search(r"(?<![\w.])open\(", source) is None, module
