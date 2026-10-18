"""Dylan: cell images go to manual review (pod contract 1.1 §7-§8, aligned with Team 12).

A cell with value metadata (vm: Place-in-Cell pictures, linked data types) or an IMAGE()
formula is one MANUAL_REVIEW finding at its #xml= cell path. Every xl/richData/* part and
xl/cellimages.xml is also one workbook-level finding (worksheet "__WORKBOOK__", target
"<part>#xml=/"). Expected targets are authored from the seeds, never from audit() output.
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
WB = "__WORKBOOK__"
RICH_PARTS = [
    ("ALT_TEXT", WB, "xl/richData/_rels/richValueRel.xml.rels#xml=/", REVIEW),
    ("ALT_TEXT", WB, "xl/richData/rdrichvalue.xml#xml=/", REVIEW),
    ("ALT_TEXT", WB, "xl/richData/rdrichvaluestructure.xml#xml=/", REVIEW),
    ("ALT_TEXT", WB, "xl/richData/richValueRel.xml#xml=/", REVIEW),
]


def cell_path(row: int, cell: int, part: str = S1) -> str:
    return f"{part}#xml=/worksheet[1]/sheetData[1]/row[{row}]/c[{cell}]"


def sheet(name: str, part: str, sheet_id: str, cells: str, **kwargs: str | Drawing) -> Sheet:
    state = kwargs.get("state")
    drawing = kwargs.get("drawing")
    return Sheet(
        name,
        part,
        sheet_id,
        cells=cells,
        state=state if isinstance(state, str) else None,
        drawing=drawing if isinstance(drawing, Drawing) else None,
    )


def test_in_cell_picture_is_reviewed_at_its_cell_and_rich_data_parts() -> None:
    cells = f'<row r="2"><c r="A2"><v>1</v></c>{rich_cell("B2", 0)}</row>'
    wb = Workbook([sheet("Inventory", S1, "1", cells)], extra_parts=rich_value_parts())
    evaluation = audit_workbook(wb)
    assert summary(evaluation) == [
        ("ALT_TEXT", "Inventory", cell_path(1, 2), REVIEW),
        *RICH_PARTS,
    ]
    cell_finding = evaluation.findings[0]
    assert cell_finding.category.value == "NON_TEXT_CONTENT"
    assert "B2" in cell_finding.message
    assert any(S1 in item and "cell images" in item for item in evaluation.limitations)


def test_image_formula_without_value_metadata_is_reviewed() -> None:
    cells = (
        '<row r="1"><c r="A1"><f>_xlfn.IMAGE("https://example.invalid/i.png")</f></c>'
        '<c r="B1"><f>SUM(1,2)</f><v>3</v></c><c r="C1"><f>myIMAGE(1)</f></c>'
        '<c r="D1"><f>image (A1)</f></c></row>'
    )
    evaluation = audit_workbook(Workbook([sheet("Inventory", S1, "1", cells)]))
    assert summary(evaluation) == [
        ("ALT_TEXT", "Inventory", cell_path(1, 1), REVIEW),
        ("ALT_TEXT", "Inventory", cell_path(1, 4), REVIEW),
    ]


def test_every_cell_image_is_reported_including_hidden_sheets() -> None:
    first = f'<row r="1">{rich_cell("A1", 0)}</row><row r="3">{rich_cell("C3", 1)}</row>'
    second = f'<row r="5">{rich_cell("D5", 2)}</row>'
    wb = Workbook(
        [
            sheet("Inventory", S1, "1", first),
            sheet("Archive", S2, "2", second, state="hidden"),
        ],
        extra_parts=rich_value_parts(images=3),
    )
    assert summary(audit_workbook(wb)) == [
        ("ALT_TEXT", "Archive", cell_path(1, 1, S2), REVIEW),
        ("ALT_TEXT", "Inventory", cell_path(1, 1), REVIEW),
        ("ALT_TEXT", "Inventory", cell_path(2, 1), REVIEW),
        *RICH_PARTS,
    ]


def test_other_findings_are_preserved_alongside_cell_images() -> None:
    drawing = Drawing("xl/drawings/drawing1.xml", drawing_xml(anchor(pic(cnvpr("2", "Logo")))))
    cells = f'<row r="1">{rich_cell("A1", 0)}</row>'
    wb = Workbook([sheet("Sheet1", S1, "1", cells, drawing=drawing)])
    assert summary(audit_workbook(wb)) == [
        ("ALT_TEXT", "Sheet1", "xl/drawings/drawing1.xml#id=2", INPUT),
        ("ALT_TEXT", "Sheet1", cell_path(1, 1), REVIEW),
        ("WORKSHEET_NAME", "Sheet1", f"{S1}#sheetId=1", INPUT),
    ]


@pytest.mark.parametrize("ref", [None, "", "2B", "A1\n"])
def test_cell_path_does_not_depend_on_the_cell_reference(ref: str | None) -> None:
    cells = f"<row><c><v>1</v></c>{rich_cell(ref, 0)}</row>"
    finding = only(audit_workbook(Workbook([sheet("Inventory", S1, "1", cells)])))
    assert (finding.target, finding.classification.value) == (cell_path(1, 2), REVIEW)


def test_cellimages_part_is_a_workbook_level_review() -> None:
    xml = b'<?xml version="1.0" encoding="UTF-8"?><cellImages xmlns="urn:x"/>'
    wb = Workbook([sheet("Inventory", S1, "1", "")], extra_parts={"xl/cellimages.xml": xml})
    assert summary(audit_workbook(wb)) == [("ALT_TEXT", WB, "xl/cellimages.xml#xml=/", REVIEW)]


def test_cells_without_cell_images_produce_no_findings() -> None:
    cells = '<row r="1"><c r="A1" cm="1"><v>1</v></c><c r="B1" t="e"><v>#VALUE!</v></c></row>'
    evaluation = audit_workbook(Workbook([sheet("Inventory", S1, "1", cells)]))
    assert evaluation.findings == [] and evaluation.limitations == []


def test_same_bytes_give_identical_cell_image_findings() -> None:
    cells = f'<row r="1">{rich_cell("A1", 0)}{rich_cell("B1", 1)}</row>'
    wb = Workbook([sheet("Inventory", S1, "1", cells)], extra_parts=rich_value_parts(2))
    assert audit_workbook(wb) == audit_workbook(wb)


def test_cells_beyond_the_per_sheet_limit_are_counted_in_a_limitation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(structure, "MAX_RICH_VALUE_CELLS_PER_SHEET", 2)
    cells = '<row r="1">' + "".join(rich_cell(f"{c}1", 0) for c in "ABCDE") + "</row>"
    evaluation = audit_workbook(Workbook([sheet("Inventory", S1, "1", cells)]))
    assert [f.target for f in evaluation.findings] == [cell_path(1, 1), cell_path(1, 2)]
    assert any("3 more cell image(s)" in item for item in evaluation.limitations)
