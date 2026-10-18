"""Dylan: in-cell pictures and other rich-value cells go to manual review (pod contract 1.1 §7).

Expected targets are authored from the seeds, never from audit() output.
"""

import pytest

from app.workbooks import structure
from tests.unit.ooxml_builder import (
    Drawing,
    Sheet,
    Workbook,
    anchor,
    cnvpr,
    drawing_xml,
    pic,
    rich_cell,
    rich_value_parts,
)
from tests.unit.test_audit_alt_text import INPUT, REVIEW, audit_workbook, only, summary

S1 = "xl/worksheets/sheet1.xml"
S2 = "xl/worksheets/sheet2.xml"


def rich_sheet(
    name: str,
    part: str,
    sheet_id: str,
    cells: str,
    *,
    state: str | None = None,
    drawing: Drawing | None = None,
) -> Sheet:
    return Sheet(name, part, sheet_id, cells=cells, state=state, drawing=drawing)


def test_in_cell_picture_is_one_manual_review_finding_at_its_cell() -> None:
    cells = f'<row r="2"><c r="A2"><v>1</v></c>{rich_cell("B2", 0)}</row>'
    wb = Workbook([rich_sheet("Inventory", S1, "1", cells)], extra_parts=rich_value_parts())
    evaluation = audit_workbook(wb)
    finding = only(evaluation)
    assert (finding.ruleId, finding.category.value) == ("ALT_TEXT", "NON_TEXT_CONTENT")
    assert (finding.worksheet, finding.target) == ("Inventory", f"{S1}#cell=B2")
    assert finding.classification.value == REVIEW
    assert "B2" in finding.message
    assert any(S1 in item and "rich values" in item for item in evaluation.limitations)


def test_every_rich_value_cell_is_reported_including_hidden_sheets() -> None:
    first = f'<row r="1">{rich_cell("A1", 0)}</row><row r="3">{rich_cell("C3", 1)}</row>'
    second = f'<row r="5">{rich_cell("D5", 2)}</row>'
    wb = Workbook(
        [
            rich_sheet("Inventory", S1, "1", first),
            rich_sheet("Archive", S2, "2", second, state="hidden"),
        ],
        extra_parts=rich_value_parts(images=3),
    )
    assert summary(audit_workbook(wb)) == [
        ("ALT_TEXT", "Archive", f"{S2}#cell=D5", REVIEW),
        ("ALT_TEXT", "Inventory", f"{S1}#cell=A1", REVIEW),
        ("ALT_TEXT", "Inventory", f"{S1}#cell=C3", REVIEW),
    ]


def test_other_findings_are_preserved_alongside_rich_values() -> None:
    drawing = Drawing("xl/drawings/drawing1.xml", drawing_xml(anchor(pic(cnvpr("2", "Logo")))))
    cells = f'<row r="1">{rich_cell("A1", 0)}</row>'
    wb = Workbook(
        [rich_sheet("Sheet1", S1, "1", cells, drawing=drawing)], extra_parts=rich_value_parts()
    )
    assert summary(audit_workbook(wb)) == [
        ("ALT_TEXT", "Sheet1", "xl/drawings/drawing1.xml#id=2", INPUT),
        ("ALT_TEXT", "Sheet1", f"{S1}#cell=A1", REVIEW),
        ("WORKSHEET_NAME", "Sheet1", f"{S1}#sheetId=1", INPUT),
    ]


@pytest.mark.parametrize("ref", [None, "", "B", "2B", "ABCD1", "A12345678", "A1\n"])
def test_missing_or_malformed_cell_reference_falls_back_to_xml_path(ref: str | None) -> None:
    cells = f"<row><c><v>1</v></c>{rich_cell(ref, 0)}</row>"
    wb = Workbook([rich_sheet("Inventory", S1, "1", cells)], extra_parts=rich_value_parts())
    finding = only(audit_workbook(wb))
    assert finding.target == f"{S1}#xml=/worksheet[1]/sheetData[1]/row[1]/c[2]"
    assert finding.classification.value == REVIEW


def test_cells_without_value_metadata_produce_no_findings() -> None:
    cells = '<row r="1"><c r="A1" cm="1"><v>1</v></c><c r="B1" t="e"><v>#VALUE!</v></c></row>'
    wb = Workbook([rich_sheet("Inventory", S1, "1", cells)])
    evaluation = audit_workbook(wb)
    assert evaluation.findings == [] and evaluation.limitations == []


def test_same_bytes_give_identical_rich_value_findings() -> None:
    cells = f'<row r="1">{rich_cell("A1", 0)}{rich_cell("B1", 1)}</row>'
    wb = Workbook([rich_sheet("Inventory", S1, "1", cells)], extra_parts=rich_value_parts(2))
    assert audit_workbook(wb) == audit_workbook(wb)


def test_cells_beyond_the_per_sheet_limit_are_counted_in_a_limitation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(structure, "MAX_RICH_VALUE_CELLS_PER_SHEET", 2)
    cells = '<row r="1">' + "".join(rich_cell(f"{c}1", 0) for c in "ABCDE") + "</row>"
    wb = Workbook([rich_sheet("Inventory", S1, "1", cells)], extra_parts=rich_value_parts())
    evaluation = audit_workbook(wb)
    assert [f.target for f in evaluation.findings] == [f"{S1}#cell=A1", f"{S1}#cell=B1"]
    assert any("3 more rich-value cell(s)" in item for item in evaluation.limitations)
