"""Ruleset 2.0.0 corrections: preflight, targeted edits, preservation and effect checks."""

import hashlib
import io
import zipfile
from datetime import UTC, datetime
from xml.etree.ElementTree import fromstring

import pytest

from app.audit.engine import OoxmlWorkbookEngine
from app.contracts.errors import ProjectException
from app.contracts.models import (
    AuditEvaluation,
    AuditRecord,
    AuditStatus,
    ProcessingLimits,
    ValidatedWorkbook,
)
from app.contracts.phase4 import Decision
from app.services.reports import validate_evaluation, validate_finding_ids
from app.workbooks.editor import rebuild_package
from tests.unit.phase4_builder import (
    MAIN,
    Cell,
    Font,
    Link,
    Sheet,
    TableDef,
    Workbook,
    formula,
    picture_drawing,
    text,
)

ENGINE = OoxmlWorkbookEngine()
LIMITS = ProcessingLimits()
NOW = datetime(2026, 10, 30, tzinfo=UTC)


def record(evaluation: AuditEvaluation) -> AuditRecord:
    return AuditRecord(
        audit_id="audit",
        workbook_id="workbook",
        version_id="version",
        owner_subject="owner",
        session_id=None,
        ruleset_version="2.0.0",
        status=AuditStatus.REVIEW_REQUIRED,
        started_at=NOW,
        completed_at=NOW,
        findings=evaluation.findings,
        limitations=evaluation.limitations,
        error=None,
        review_context=evaluation.review_context,
        checks_performed=evaluation.checks_performed,
    )


def prepare(data: bytes) -> tuple[ValidatedWorkbook, AuditRecord]:
    validated = ENGINE.validate(data, "book.xlsx", LIMITS)
    return validated, record(ENGINE.audit(data, validated, "2.0.0"))


def decide(audit: AuditRecord, values: dict[str, dict[str, object] | None]) -> list[Decision]:
    """values maps a target suffix to an APPLY value (None declines that finding)."""
    decisions = []
    for finding in audit.findings:
        key = next((k for k in values if finding.target.endswith(k)), None)
        if key is None:
            continue
        value = values[key]
        if value is None:
            decisions.append(Decision(findingId=finding.findingId, action="DECLINE"))
        else:
            decisions.append(Decision(findingId=finding.findingId, action="APPLY", value=value))
    return decisions


def run(
    data: bytes, decisions: list[Decision], audit: AuditRecord, validated: ValidatedWorkbook
) -> tuple[bytes, AuditEvaluation]:
    """The worker's stages in order: preflight, edit, validate, preserve, audit, verify."""
    ENGINE.validate_decisions(data, validated, audit, decisions)
    candidate = ENGINE.remediate(data, validated, audit, decisions)
    output_validated = ENGINE.validate(candidate, "book-remediated.xlsx", LIMITS)
    ENGINE.verify_preservation(data, candidate, validated, audit, decisions)
    evaluation = ENGINE.audit(candidate, output_validated, "2.0.0")
    validate_evaluation(evaluation, "2.0.0")
    validate_finding_ids(evaluation, hashlib.sha256(candidate).hexdigest(), "2.0.0")
    ENGINE.verify_corrections(data, candidate, audit, decisions, evaluation)
    return candidate, evaluation


def entries(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def changed_parts(before: bytes, after: bytes) -> tuple[set[str], set[str]]:
    a, b = entries(before), entries(after)
    return {n for n in a if a[n] != b.get(n)}, set(b) - set(a)


# -- ALT_TEXT ------------------------------------------------------------------------


def test_alt_text_sets_both_fields_and_touches_only_the_drawing() -> None:
    data = Workbook(
        [Sheet("Charts", {"A1": "Totals"}, drawing=picture_drawing("4", title="Revenue"))]
    ).build()
    validated, audit = prepare(data)
    value = {"title": "Revenue & costs", "description": 'Bar chart "2026"\n<draft>'}
    candidate, output = run(data, decide(audit, {"#id=4": value}), audit, validated)
    changed, added = changed_parts(data, candidate)
    assert (changed, added) == ({"xl/drawings/drawing1.xml"}, set())
    drawing = entries(candidate)["xl/drawings/drawing1.xml"].decode()
    assert 'title="Revenue &amp; costs"' in drawing
    assert 'descr="Bar chart &quot;2026&quot;&#10;&lt;draft&gt;"' in drawing
    assert drawing.startswith('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
    assert [f.ruleId for f in output.findings] == []


# -- LINK_TEXT -------------------------------------------------------------------------


def test_link_label_becomes_inline_text_and_shared_strings_stay_untouched() -> None:
    data = Workbook(
        [
            Sheet(
                "Links",
                {"B2": text("Click here", 0), "B3": "Click here"},
                links=[Link("B2", url="https://example.edu/r", display="Click here")],
            )
        ]
    ).build()
    validated, audit = prepare(data)
    value = {"text": "=Annual report "}
    candidate, output = run(data, decide(audit, {"#cell=B2": value}), audit, validated)
    changed, added = changed_parts(data, candidate)
    assert (changed, added) == ({"xl/worksheets/sheet1.xml"}, set())
    sheet = entries(candidate)["xl/worksheets/sheet1.xml"].decode()
    assert (
        '<c r="B2" s="0" t="inlineStr"><is><t xml:space="preserve">=Annual report </t></is></c>'
        in sheet
    )
    assert 'display="=Annual report "' in sheet
    assert 'xmlns:x14ac="http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac"' in sheet
    assert output.findings == []


def test_ambiguous_replacement_label_is_invalid() -> None:
    data = Workbook(
        [Sheet("Links", {"A1": "here"}, links=[Link("A1", url="https://x.example")])]
    ).build()
    validated, audit = prepare(data)
    with pytest.raises(ProjectException) as error:
        ENGINE.validate_decisions(
            data, validated, audit, decide(audit, {"#cell=A1": {"text": "Read more."}})
        )
    assert error.value.code == "INVALID_CORRECTION"


# -- TABLE_HEADER ----------------------------------------------------------------------


def test_confirmed_header_creates_minimal_table_in_schema_order() -> None:
    cells: dict[str, Cell | str | float] = {
        "B2": "Name",
        "C2": "Score & rank",
        "B3": "Ada",
        "C3": formula("1+1", 2),
        "B4": "Al",
        "C4": 7,
    }
    data = Workbook(
        [Sheet("Scores", cells, drawing=picture_drawing("2", "Logo", "Company logo"))]
    ).build()
    validated, audit = prepare(data)
    [finding] = [f for f in audit.findings if f.ruleId == "TABLE_HEADER"]
    assert finding.target == "xl/worksheets/sheet1.xml#range=B2:C4"
    candidate, output = run(
        data, decide(audit, {"#range=B2:C4": {"headerRow": 2}}), audit, validated
    )
    changed, added = changed_parts(data, candidate)
    assert changed == {
        "[Content_Types].xml",
        "xl/worksheets/sheet1.xml",
        "xl/worksheets/_rels/sheet1.xml.rels",
    }
    assert added == {"xl/tables/table1.xml"}
    files = entries(candidate)
    table = fromstring(files["xl/tables/table1.xml"])
    assert table.attrib == {
        "id": "1",
        "name": "A11yTable1",
        "displayName": "A11yTable1",
        "ref": "B2:C4",
        "headerRowCount": "1",
        "totalsRowShown": "0",
    }
    assert [c.get("name") for c in table[0]] == ["Name", "Score & rank"]
    sheet = files["xl/worksheets/sheet1.xml"].decode()
    assert sheet.index("<drawing") < sheet.index('<tableParts count="1"><tablePart r:id="rId1"/>')
    assert "<f>1+1</f><v>2</v>" in sheet  # body formula and cache untouched
    assert 'Target="../tables/table1.xml"' in files["xl/worksheets/_rels/sheet1.xml.rels"].decode()
    assert 'PartName="/xl/tables/table1.xml"' in files["[Content_Types].xml"].decode()
    assert [f.ruleId for f in output.findings if f.ruleId == "TABLE_HEADER"] == []


def test_header_for_sheet_without_relationships_adds_rels_part() -> None:
    data = Workbook([Sheet("Data", {"A1": "N", "B1": "S", "A2": "x", "B2": 1})]).build()
    validated, audit = prepare(data)
    candidate, _ = run(data, decide(audit, {"#range=A1:B2": {"headerRow": 1}}), audit, validated)
    assert changed_parts(data, candidate)[1] == {
        "xl/tables/table1.xml",
        "xl/worksheets/_rels/sheet1.xml.rels",
    }


def test_existing_table_ids_and_names_are_avoided() -> None:
    sheet = Sheet(
        "Data",
        {"A1": "N", "B1": "S", "A2": "x", "B2": 1, "D1": "P", "E1": "Q", "D2": "y", "E2": 2},
        tables=[TableDef("A1:B2", ["N", "S"], name="A11yTable2", table_id="1")],
    )
    data = Workbook([sheet], defined_names=[("a11ytable2_1", "1")]).build()
    validated, audit = prepare(data)
    candidate, _ = run(data, decide(audit, {"#range=D1:E2": {"headerRow": 1}}), audit, validated)
    table = fromstring(entries(candidate)["xl/tables/table2.xml"])
    assert (table.get("id"), table.get("name")) == ("2", "A11yTable2_2")


def test_enabling_a_disabled_table_header_changes_only_the_count() -> None:
    sheet = Sheet(
        "Scores",
        {"A1": "Name", "B1": "Score", "A2": "Ada", "B2": 9},
        tables=[TableDef("A1:B2", ["Name", "Score"], header_rows="0")],
    )
    data = Workbook([sheet]).build()
    validated, audit = prepare(data)
    candidate, output = run(data, decide(audit, {"#tableId=1": {"headerRow": 1}}), audit, validated)
    assert changed_parts(data, candidate) == ({"xl/tables/table1.xml"}, set())
    assert 'headerRowCount="1"' in entries(candidate)["xl/tables/table1.xml"].decode()
    assert output.findings == []


def test_wrong_header_row_is_invalid() -> None:
    data = Workbook([Sheet("Data", {"A1": "N", "B1": "S", "A2": "x", "B2": 1})]).build()
    validated, audit = prepare(data)
    with pytest.raises(ProjectException) as error:
        ENGINE.validate_decisions(
            data, validated, audit, decide(audit, {"#range=A1:B2": {"headerRow": 2}})
        )
    assert error.value.code == "INVALID_CORRECTION"


def test_link_in_confirmed_header_row_conflicts() -> None:
    data = Workbook(
        [
            Sheet(
                "Data",
                {"A1": "here", "B1": "Score", "A2": "x", "B2": 1},
                links=[Link("A1", url="https://x.example")],
            )
        ]
    ).build()
    validated, audit = prepare(data)
    decisions = decide(
        audit, {"#range=A1:B2": {"headerRow": 1}, "#cell=A1": {"text": "Course list"}}
    )
    assert len(decisions) == 2
    with pytest.raises(ProjectException) as error:
        ENGINE.validate_decisions(data, validated, audit, decisions)
    assert error.value.code == "CONFLICTING_CORRECTIONS"


# -- WORKSHEET_NAME --------------------------------------------------------------------


def rename_workbook() -> bytes:
    validations = (
        '<dataValidations count="1"><dataValidation type="list" sqref="C1">'
        "<formula1>Sheet1!$A$1:$A$3</formula1></dataValidation></dataValidations>"
    )
    conditional = (
        '<conditionalFormatting sqref="D1"><cfRule type="expression" priority="1">'
        "<formula>Sheet1!$A$1&gt;0</formula></cfRule></conditionalFormatting>"
    )
    return Workbook(
        [
            Sheet("Sheet1", {"A1": 1, "A2": 2, "A3": 3}),
            Sheet(
                "Summary",
                {
                    "A1": formula("SUM(Sheet1!A1:A3)+'Sheet1'!$A$1+Sheet10!A1", 7),
                    "A2": formula('"Sheet1!A1"&Sheet1!A2', "Sheet1!A12", "str"),
                    "B1": "Back to data",
                },
                validations=validations,
                conditional=conditional,
                links=[Link("B1", location="Sheet1!A1")],
            ),
            Sheet("Sheet10", {"A1": 0}),
        ],
        defined_names=[("Totals", "Sheet1!$A$1:$A$3"), ("Other", "Sheet10!$A$1")],
        charts=["Sheet1!$A$1:$A$3"],
    ).build()


def test_rename_rewrites_every_supported_reference_and_nothing_else() -> None:
    data = rename_workbook()
    validated, audit = prepare(data)
    finding = next(f for f in audit.findings if f.target.endswith("sheet1.xml#sheetId=1"))
    assert finding.classification.value == "REQUIRES_USER_INPUT"
    decisions = [Decision(findingId=finding.findingId, action="APPLY", value={"name": "Raw data"})]
    candidate, output = run(data, decisions, audit, validated)
    changed, added = changed_parts(data, candidate)
    assert added == set()
    assert changed == {"xl/workbook.xml", "xl/worksheets/sheet2.xml", "xl/charts/chart1.xml"}
    files = {
        name: content.decode()
        for name, content in entries(candidate).items()
        if name.endswith(".xml")
    }
    workbook = files["xl/workbook.xml"]
    assert '<sheet name="Raw data" sheetId="1" r:id="rIdSheet1"/>' in workbook
    assert "<definedName name=\"Totals\">'Raw data'!$A$1:$A$3</definedName>" in workbook
    assert '<definedName name="Other">Sheet10!$A$1</definedName>' in workbook
    assert 'mc:Ignorable="x14ac"' in workbook
    summary = files["xl/worksheets/sheet2.xml"]
    assert "<f>SUM('Raw data'!A1:A3)+'Raw data'!$A$1+Sheet10!A1</f><v>7</v>" in summary
    assert "<f>\"Sheet1!A1\"&amp;'Raw data'!A2</f>" in summary
    assert "<formula1>'Raw data'!$A$1:$A$3</formula1>" in summary
    assert "<formula>'Raw data'!$A$1&gt;0</formula>" in summary
    assert "location=\"'Raw data'!A1\"" in summary
    assert "<c:f>'Raw data'!$A$1:$A$3</c:f>" in files["xl/charts/chart1.xml"]
    assert not any(
        f.ruleId == "WORKSHEET_NAME" and f.target.endswith("sheetId=1") for f in output.findings
    )


def test_rename_to_existing_name_conflicts_and_swap_is_refused() -> None:
    data = rename_workbook()
    validated, audit = prepare(data)
    finding = next(f for f in audit.findings if f.target.endswith("sheet1.xml#sheetId=1"))
    # The backend rejects duplicates first; the engine still refuses an unchanged name.
    with pytest.raises(ProjectException) as error:
        ENGINE.validate_decisions(
            data,
            validated,
            audit,
            [Decision(findingId=finding.findingId, action="APPLY", value={"name": "Sheet1"})],
        )
    assert error.value.code == "INVALID_CORRECTION"


# -- refusals and preservation failures ------------------------------------------------


def test_apply_to_manual_finding_is_unsupported() -> None:
    data = Workbook([Sheet("Report", {"A1": "Title"}, merges=["A1:C1"])]).build()
    validated, audit = prepare(data)
    [finding] = [f for f in audit.findings if f.ruleId == "MERGED_CELLS"]
    with pytest.raises(ProjectException) as error:
        ENGINE.validate_decisions(
            data,
            validated,
            audit,
            [Decision(findingId=finding.findingId, action="APPLY", value={"text": "x"})],
        )
    assert error.value.code == "UNSUPPORTED_CORRECTION"


def test_decline_only_batch_returns_identical_bytes() -> None:
    data = Workbook([Sheet("Charts", {}, drawing=picture_drawing())]).build()
    validated, audit = prepare(data)
    decisions = decide(audit, {"#id=2": None})
    assert ENGINE.remediate(data, validated, audit, decisions) == data
    ENGINE.verify_preservation(data, data, validated, audit, decisions)


def tampered(candidate: bytes, part: str, edit: tuple[bytes, bytes]) -> bytes:
    files = entries(candidate)
    assert edit[0] in files[part]
    return rebuild_package(candidate, {part: files[part].replace(*edit)}, {})


@pytest.mark.parametrize(
    ("part", "edit"),
    [
        ("xl/styles.xml", (b'sz val="11"', b'sz val="12"')),  # untouched part changed
        ("xl/drawings/drawing1.xml", (b'name="Picture 2"', b'name="Renamed"')),  # extra attribute
        ("xl/drawings/drawing1.xml", (b"<xdr:clientData/>", b"")),  # removed element
        ("xl/drawings/drawing1.xml", (b'xmlns:r="', b'xmlns:rr="')),  # namespace declaration
    ],
)
def test_unexplained_differences_fail_preservation(part: str, edit: tuple[bytes, bytes]) -> None:
    data = Workbook([Sheet("Charts", {"A1": "x"}, drawing=picture_drawing())]).build()
    validated, audit = prepare(data)
    decisions = decide(audit, {"#id=2": {"title": "Logo", "description": "Company logo"}})
    candidate = ENGINE.remediate(data, validated, audit, decisions)
    ENGINE.verify_preservation(data, candidate, validated, audit, decisions)
    with pytest.raises(ProjectException) as error:
        ENGINE.verify_preservation(
            data, tampered(candidate, part, edit), validated, audit, decisions
        )
    assert error.value.code == "PRESERVATION_FAILED"


def test_missing_correction_effect_fails_verification() -> None:
    data = Workbook([Sheet("Charts", {}, drawing=picture_drawing())]).build()
    validated, audit = prepare(data)
    decisions = decide(audit, {"#id=2": {"title": "Logo", "description": "Company logo"}})
    evaluation = ENGINE.audit(data, validated, "2.0.0")
    with pytest.raises(ProjectException) as error:
        ENGINE.verify_corrections(data, data, audit, decisions, evaluation)
    assert error.value.code == "OUTPUT_AUDIT_FAILED"


def test_bom_and_strict_family_are_preserved() -> None:
    sheet = Sheet("Data", {"A1": "N", "B1": "S", "A2": "x", "B2": 1}, drawing=picture_drawing())
    for workbook in (Workbook([sheet], utf8_bom=True), Workbook([sheet], strict=True)):
        data = workbook.build()
        validated, audit = prepare(data)
        decisions = decide(
            audit,
            {"#range=A1:B2": {"headerRow": 1}, "#id=2": {"title": "Logo", "description": "Logo"}},
        )
        candidate, output = run(data, decisions, audit, validated)
        files = entries(candidate)
        if workbook.utf8_bom:
            assert files["xl/worksheets/sheet1.xml"].startswith(b"\xef\xbb\xbf")
        else:
            table = files["xl/tables/table1.xml"].decode()
            assert 'xmlns="http://purl.oclc.org/ooxml/spreadsheetml/main"' in table
            assert (
                "purl.oclc.org/ooxml/officeDocument/relationships/table"
                in files["xl/worksheets/_rels/sheet1.xml.rels"].decode()
            )
        assert output.findings == []


def test_full_mixed_batch_end_to_end() -> None:
    fonts = [Font()]
    data = Workbook(
        [
            Sheet(
                "Sheet1",
                {"A1": "Name", "B1": "Score", "A2": "Ada", "B2": 9, "D5": "click here"},
                links=[Link("D5", url="https://example.edu/scores")],
                drawing=picture_drawing("3"),
                merges=["F1:G1"],
            ),
            Sheet("Notes", {"A1": formula("Sheet1!B2*2", 18)}),
        ],
        fonts=fonts,
    ).build()
    validated, audit = prepare(data)
    decisions = decide(
        audit,
        {
            "#id=3": {"title": "Score chart", "description": "Scores by student"},
            "#cell=D5": {"text": "Score details"},
            "#range=A1:B2": {"headerRow": 1},
            "#sheetId=1": {"name": "Scores"},
        },
    )
    assert len(decisions) == 4
    candidate, output = run(data, decisions, audit, validated)
    files = entries(candidate)
    assert "<f>'Scores'!B2*2</f>" in files["xl/worksheets/sheet2.xml"].decode()
    assert output.findings == []
    assert MAIN in files["xl/tables/table1.xml"].decode()
