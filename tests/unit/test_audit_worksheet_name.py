"""Dylan: WORKSHEET_NAME detection, including hidden sheets and leading-zero suffixes."""

import pytest

from app.audit.engine import OoxmlWorkbookEngine
from app.contracts.models import ProcessingLimits
from tests.unit.ooxml_builder import Sheet, Workbook

ENGINE = OoxmlWorkbookEngine()

GENERIC = [
    "Sheet1",
    "sheet2",
    "SHEET3",
    "Sheet01",
    "Sheet007",
    " Sheet3 ",
    "\u3000Sheet4\u00a0",
    "\tSheet12\n",
    "Sheet99999999999999999999",
]
DESCRIPTIVE = [
    "Sheet0",
    "Sheet00",
    "Sheet",
    "Expenses",
    "Q1 Budget",
    "Sheet 1",
    "Sheet1a",
    "Sheet-1",
    "MySheet1",
    "Sheet\u0661",
    "Sheet\uff11",
    "\u017fheet1",
    "She\u200bet1",
    "Sheet1\u200b",
]


def audit_names(*names: str, hidden: int | None = None):  # type: ignore[no-untyped-def]
    sheets = [
        Sheet(name, f"xl/worksheets/w{i}.xml", str(i + 10), state="hidden" if hidden == i else None)
        for i, name in enumerate(names)
    ]
    data = Workbook(sheets).build()
    validated = ENGINE.validate(data, "names.xlsx", ProcessingLimits())
    return ENGINE.audit(data, validated, "1.0.0")


@pytest.mark.parametrize("name", GENERIC)
def test_generic_names_are_flagged_with_raw_name_preserved(name: str) -> None:
    findings = audit_names(name).findings
    assert len(findings) == 1
    finding = findings[0]
    assert (finding.ruleId, finding.category.value, finding.classification.value) == (
        "WORKSHEET_NAME",
        "WORKSHEET_NAME",
        "REQUIRES_USER_INPUT",
    )
    assert finding.worksheet == name  # untrimmed raw spelling
    assert finding.target == "xl/worksheets/w0.xml#sheetId=10"
    assert "generic default name" in finding.message
    assert finding.recommendation.startswith("Rename the worksheet")


@pytest.mark.parametrize("name", DESCRIPTIVE)
def test_descriptive_and_edge_names_are_not_flagged(name: str) -> None:
    assert audit_names(name).findings == []


def test_hidden_generic_sheet_is_flagged_and_target_uses_workbook_sheet_id() -> None:
    findings = audit_names("Summary", "Sheet2", "Sheet5", hidden=1).findings
    assert [(f.worksheet, f.target) for f in findings] == [
        ("Sheet2", "xl/worksheets/w1.xml#sheetId=11"),
        ("Sheet5", "xl/worksheets/w2.xml#sheetId=12"),
    ]


def test_sheet_name_does_not_need_to_match_part_name() -> None:
    sheets = [Sheet("Sheet1", "xl/worksheets/sheet7.xml", "3")]
    data = Workbook(sheets).build()
    finding = ENGINE.audit(data, ENGINE.validate(data, "a.xlsx", ProcessingLimits()), "1.0.0")
    assert [f.target for f in finding.findings] == ["xl/worksheets/sheet7.xml#sheetId=3"]
