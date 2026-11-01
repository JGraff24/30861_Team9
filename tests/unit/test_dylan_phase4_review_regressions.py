"""Independent regressions from Phase 4 integration review."""

from pathlib import Path
from xml.etree.ElementTree import fromstring

import pytest

from app.contracts.errors import ProjectException
from app.workbooks.editor import rebuild_package
from tests.unit.phase4_builder import Font, Sheet, Workbook, formula, number, picture_drawing, text
from tests.unit.test_dylan_phase4_corrections import ENGINE, decide, entries, prepare, run, tampered
from tests.unit.test_dylan_phase4_rules import audit as audited


def test_rename_resolves_xml_character_references_before_rewriting_formula() -> None:
    data = Workbook([Sheet("Sheet1", {"A1": 3, "B2": formula("Sheet1!A1", 3)})]).build()
    part = "xl/worksheets/sheet1.xml"
    data = tampered(data, part, (b"Sheet1!A1", b"Sh&#101;et1!A1"))
    validated, audit = prepare(data)
    decisions = decide(audit, {"#sheetId=1": {"name": "Expenses"}})
    candidate, _ = run(data, decisions, audit, validated)
    output = fromstring(entries(candidate)[part])
    assert [node.text for node in output.iter() if node.tag.endswith("}f")] == ["'Expenses'!A1"]


def test_preservation_rejects_a_missing_required_rename_reference_update() -> None:
    data = Workbook([Sheet("Sheet1", {"A1": 3, "B2": formula("Sheet1!A1", 3)})]).build()
    validated, audit = prepare(data)
    decisions = decide(audit, {"#sheetId=1": {"name": "Expenses"}})
    candidate, _ = run(data, decisions, audit, validated)
    part = "xl/worksheets/sheet1.xml"
    candidate = rebuild_package(candidate, {part: entries(data)[part]}, {})
    with pytest.raises(ProjectException) as error:
        ENGINE.verify_preservation(data, candidate, validated, audit, decisions)
    assert error.value.code == "PRESERVATION_FAILED"


@pytest.mark.parametrize("markup", [b"<!--retain-comment-->", b"<?retain instruction?>"])
@pytest.mark.parametrize("placement", ["prolog", "root", "epilog"])
def test_preservation_compares_comments_and_processing_instructions_in_edited_parts(
    markup: bytes,
    placement: str,
) -> None:
    data = Workbook([Sheet("Charts", {}, drawing=picture_drawing("1"))]).build()
    part = "xl/drawings/drawing1.xml"
    original = entries(data)[part]
    if placement == "prolog":
        updated = original.replace(b"?>", b"?>" + markup, 1)
    elif placement == "epilog":
        updated = original + markup
    else:
        updated = original.replace(b"</xdr:wsDr>", markup + b"</xdr:wsDr>")
    data = rebuild_package(data, {part: updated}, {})
    validated, audit = prepare(data)
    decisions = decide(audit, {"#id=1": {"title": "Revenue", "description": "Quarterly revenue"}})
    candidate, _ = run(data, decisions, audit, validated)
    assert markup in entries(candidate)[part]
    candidate = tampered(candidate, part, (markup, b""))
    with pytest.raises(ProjectException) as error:
        ENGINE.verify_preservation(data, candidate, validated, audit, decisions)
    assert error.value.code == "PRESERVATION_FAILED"


@pytest.mark.parametrize(
    "edit",
    [
        (b'tableColumns count="2"', b'tableColumns count="99"'),
        (b'tableColumn id="2"', b'tableColumn id="1"'),
        (b'totalsRowShown="0"', b'totalsRowShown="1"'),
    ],
)
def test_preservation_rejects_unapproved_new_table_metadata(edit: tuple[bytes, bytes]) -> None:
    data = Workbook([Sheet("Data", {"A1": "Name", "B1": "Amount", "A2": "Ada", "B2": 3})]).build()
    validated, audit = prepare(data)
    decisions = decide(audit, {"#range=A1:B2": {"headerRow": 1}})
    candidate, _ = run(data, decisions, audit, validated)
    candidate = tampered(candidate, "xl/tables/table1.xml", edit)
    with pytest.raises(ProjectException) as error:
        ENGINE.verify_preservation(data, candidate, validated, audit, decisions)
    assert error.value.code == "PRESERVATION_FAILED"


def test_manual_worksheet_review_retains_known_raw_name() -> None:
    data = Workbook([Sheet(" Sheet1 ", {"A1": formula('INDIRECT("A1")', 1)})]).build()
    _, audit = prepare(data)
    finding = next(finding for finding in audit.findings if finding.ruleId == "WORKSHEET_NAME")
    assert finding.classification.value == "MANUAL_REVIEW"
    item = next(item for item in audit.review_context if item.findingId == finding.findingId)
    assert item.allowedActions == ["DECLINE", "MANUAL_REVIEW"]
    assert item.currentValue == {"name": " Sheet1 "}


def test_manual_alt_review_retains_known_title_and_absent_description() -> None:
    data = Workbook(
        [Sheet("Charts", {}, drawing=picture_drawing("1", title="Existing title"))],
        extra_parts={"_xmlsignatures/sig1.xml": b"<Signature/>"},
    ).build()
    _, audit = prepare(data)
    finding = next(finding for finding in audit.findings if finding.ruleId == "ALT_TEXT")
    assert finding.classification.value == "MANUAL_REVIEW"
    item = next(item for item in audit.review_context if item.findingId == finding.findingId)
    assert item.allowedActions == ["DECLINE", "MANUAL_REVIEW"]
    assert item.currentValue == {"title": "Existing title", "description": None}


@pytest.mark.parametrize(
    "expression",
    ["SUM('Sheet1:Sheet3'!A1)", "_xlfn._xlws.FILTER(Sheet1!A1:A2,Sheet1!A1:A2>0)"],
)
def test_unsupported_reference_profiles_are_manual_before_acceptance(expression: str) -> None:
    data = Workbook(
        [
            Sheet("Sheet1", {"A1": 1, "A2": 2}),
            Sheet("Sheet3", {"A1": 3}),
            Sheet("Summary", {"A1": formula(expression, 4)}),
        ]
    ).build()
    evaluation = audited(data)
    renames = [finding for finding in evaluation.findings if finding.ruleId == "WORKSHEET_NAME"]
    assert len(renames) == 2
    assert {finding.classification.value for finding in renames} == {"MANUAL_REVIEW"}
    assert any("rename" in limitation.lower() for limitation in evaluation.limitations)


def test_omitted_cell_font_cannot_silently_discard_a_nondefault_base_font() -> None:
    data = Workbook(
        [Sheet("Data", {"A1": text("Content", 1)})],
        fonts=[Font(), Font(color='rgb="FFFF0000"')],
        formats=[(0, 0), (1, 0)],
    ).build()
    part = "xl/styles.xml"
    styles = (
        entries(data)[part]
        .replace(
            b'<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>',
            b'<cellStyleXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>'
            b'<xf numFmtId="0" fontId="1" fillId="0" borderId="0"/>',
        )
        .replace(
            b'<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0"/>', b'<xf xfId="1"/>'
        )
    )
    data = rebuild_package(data, {part: styles}, {})
    evaluation = audited(data)
    colors = [finding for finding in evaluation.findings if finding.ruleId == "COLOR_CONTRAST"]
    assert len(colors) == 1
    assert colors[0].target == "xl/worksheets/sheet1.xml#cell=A1"
    assert colors[0].classification.value == "MANUAL_REVIEW"
    assert any("inherit" in limitation.lower() for limitation in evaluation.limitations)


def test_drawing_hyperlink_destination_blocks_unsupported_reference_rewrites() -> None:
    drawing = picture_drawing("1", "Logo", "Company logo").replace(
        '<xdr:cNvPr id="1" name="Picture 1" title="Logo" descr="Company logo"/>',
        '<xdr:cNvPr id="1" name="Picture 1" title="Logo" descr="Company logo">'
        '<a:hlinkClick r:id="rIdLink"/></xdr:cNvPr>',
    )
    relationships = (
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rIdImage" Type="http://schemas.openxmlformats.org/officeDocument/'
        b'2006/relationships/image" Target="../media/image1.png"/>'
        b'<Relationship Id="rIdLink" Type="http://schemas.openxmlformats.org/officeDocument/'
        b'2006/relationships/hyperlink" Target="#Sheet1!A1" TargetMode="External"/>'
        b"</Relationships>"
    )
    data = Workbook(
        [Sheet("Sheet1", {"A1": "Logo"}, drawing=drawing)],
        extra_parts={"xl/drawings/_rels/drawing1.xml.rels": relationships},
    ).build()
    evaluation = audited(data)
    rename = next(finding for finding in evaluation.findings if finding.ruleId == "WORKSHEET_NAME")
    assert rename.classification.value == "MANUAL_REVIEW"
    assert any("drawing" in limitation.lower() for limitation in evaluation.limitations)


def test_required_rename_fixture_supports_known_x14_conditional_formula() -> None:
    data = (
        Path(__file__).parents[1] / "fixtures/phase4/synthetic/rename_references.xlsx"
    ).read_bytes()
    validated, audit = prepare(data)
    assert [(finding.ruleId, finding.classification.value) for finding in audit.findings] == [
        ("WORKSHEET_NAME", "REQUIRES_USER_INPUT")
    ]
    decisions = decide(audit, {"#sheetId=1": {"name": "Data source"}})
    candidate, _ = run(data, decisions, audit, validated)
    root = fromstring(entries(candidate)["xl/worksheets/sheet2.xml"])
    formulas = [
        node.text
        for node in root.iter()
        if node.tag == "{http://schemas.microsoft.com/office/excel/2006/main}f"
    ]
    assert formulas == ["'Data source'!$B$2>5"]


def test_known_x14_conditional_color_still_requires_review_after_safe_rename() -> None:
    data = (
        Path(__file__).parents[1] / "fixtures/phase4/synthetic/rename_references.xlsx"
    ).read_bytes()
    data = tampered(
        data,
        "xl/worksheets/sheet2.xml",
        (b"<font><b/></font>", b'<font><color rgb="FFFF0000"/></font>'),
    )
    validated, audit = prepare(data)
    colors = [finding.target for finding in audit.findings if finding.ruleId == "COLOR_CONTRAST"]
    assert colors == ["xl/worksheets/sheet2.xml#conditional=D1:D1"]
    decisions = decide(audit, {"#sheetId=1": {"name": "Data source"}})
    _, output = run(data, decisions, audit, validated)
    assert [
        finding.target for finding in output.findings if finding.ruleId == "COLOR_CONTRAST"
    ] == colors


@pytest.mark.parametrize(
    "edit",
    [
        (b"<font><b/></font>", b'<font><extLst><ext uri="unrecognized"/></extLst></font>'),
        (b"Sheet1!$B$2&gt;5", b'INDIRECT("Sheet1!B2")'),
    ],
)
def test_opaque_or_constructed_x14_reference_extensions_remain_manual(
    edit: tuple[bytes, bytes],
) -> None:
    data = (
        Path(__file__).parents[1] / "fixtures/phase4/synthetic/rename_references.xlsx"
    ).read_bytes()
    data = tampered(data, "xl/worksheets/sheet2.xml", edit)
    evaluation = audited(data)
    rename = next(finding for finding in evaluation.findings if finding.ruleId == "WORKSHEET_NAME")
    assert rename.classification.value == "MANUAL_REVIEW"


@pytest.mark.parametrize("visual", [False, True])
def test_conditional_rendering_limitation_is_present_once_only_when_applicable(
    visual: bool,
) -> None:
    conditional = (
        '<conditionalFormatting sqref="A1 A1"><cfRule type="cellIs" dxfId="0" '
        'priority="1" operator="greaterThan"><formula>100</formula></cfRule>'
        "</conditionalFormatting>"
    )
    differential = (
        '<dxf><font><color rgb="FF000000"/></font></dxf>'
        if visual
        else "<dxf><font><b/></font></dxf>"
    )
    # The condition is false for this seed. A visual rule still needs review;
    # the engine must neither evaluate it nor report a guessed static ratio.
    data = Workbook(
        [Sheet("Colors", {"A1": number(0, 0)}, conditional=conditional)],
        fonts=[Font(color='rgb="FFFFFFFF"')],
        dxfs=[differential],
    ).build()
    evaluation = audited(data)
    limitations = [item for item in evaluation.limitations if "Conditional formatting" in item]
    findings = [finding for finding in evaluation.findings if finding.ruleId == "COLOR_CONTRAST"]
    if visual:
        assert len(limitations) == 1
        assert "not evaluated" in limitations[0] and "manual" in limitations[0]
        assert [finding.target for finding in findings] == [
            "xl/worksheets/sheet1.xml#conditional=A1:A1"
        ]
        assert all("contrast is" not in finding.message for finding in findings)
    else:
        assert limitations == []
        assert [finding.target for finding in findings] == ["xl/worksheets/sheet1.xml#cell=A1"]
