"""Independent-test-side diagnostic for the outstanding worksheet-rename discrepancy.

The normative `rename_references` fixture puts eight reference-bearing structures in one
workbook, so a single disagreement (a classification, a rewrite form or a refused APPLY) hides
which structure caused it. These variants each keep the same base workbook (one generic
`Sheet1` plus a `Report` sheet) and exactly one reference kind, written from the same literal
XML helpers as the reviewed Phase 4 fixtures. Expectations are authored here from that XML and
specification 2.1.0 sections 7.3 and 8.4, never from an engine.

This is diagnostic evidence for the joint investigation, not part of the frozen suite: it does
not change any rule, finding identity, fixture or expected result (specification 3.0.0 §2).
"""

from dataclasses import dataclass, field
from typing import Any

from tester.phase4.workbooks import (
    X14,
    X14_CF_URI,
    XM,
    Package,
    anchor,
    bar_chart,
    chart_frame,
    drawing,
    formula,
    number,
    relationships,
    rows,
    text,
    worksheet,
)

NEW_NAME = "Data source"
TARGET = "xl/worksheets/sheet1.xml#sheetId=1"


@dataclass(frozen=True)
class Variant:
    name: str
    description: str
    data: bytes
    # Expected rewritten references, in the comparator's reference format.
    references: list[dict[str, Any]] = field(default_factory=list)
    # Team 9's reading of §8.4; a variant flagged here is a known interpretation question.
    interpretation_question: str | None = None


def _base(name: str, report: str, **sheet2: Any) -> Package:
    package = Package(name)
    package.sheet("Sheet1", 1, "xl/worksheets/sheet1.xml")
    package.sheet("Report", 2, "xl/worksheets/sheet2.xml")
    package.add("xl/worksheets/sheet1.xml", worksheet(rows(number("B2", 10), number("B3", 6))))
    package.add("xl/worksheets/sheet2.xml", worksheet(report, **sheet2))
    return package


def variants() -> list[Variant]:
    found: list[Variant] = []

    def add(
        name: str,
        description: str,
        package: Package,
        refs: list[dict[str, Any]],
        question: str | None = None,
    ) -> None:
        found.append(Variant(name, description, package.build(), refs, question))

    add(
        "no_references",
        "Generic Sheet1 with no reference to it anywhere (control).",
        _base("rename_probe_none", rows(text("A1", "Notes"))),
        [],
    )
    add(
        "formula_unquoted",
        "Report!A1 =Sheet1!B2.",
        _base("rename_probe_unquoted", rows(formula("A1", "Sheet1!B2", 10))),
        [
            {
                "part": "xl/worksheets/sheet2.xml",
                "kind": "cell_formula",
                "ref": "A1",
                "formula": "'Data source'!B2",
            }
        ],
    )
    add(
        "formula_quoted",
        "Report!A1 ='Sheet1'!B3.",
        _base("rename_probe_quoted", rows(formula("A1", "'Sheet1'!B3", 6))),
        [
            {
                "part": "xl/worksheets/sheet2.xml",
                "kind": "cell_formula",
                "ref": "A1",
                "formula": "'Data source'!B3",
            }
        ],
    )
    add(
        "formula_range",
        "Report!A1 =SUM(Sheet1!B2:B3).",
        _base("rename_probe_range", rows(formula("A1", "SUM(Sheet1!B2:B3)", 16))),
        [
            {
                "part": "xl/worksheets/sheet2.xml",
                "kind": "cell_formula",
                "ref": "A1",
                "formula": "SUM('Data source'!B2:B3)",
            }
        ],
    )
    literal = _base(
        "rename_probe_literals",
        rows(
            text("A1", "Sheet1!B2"),
            formula("A2", 'LEN("Sheet1")', 6),
        ),
    )
    add(
        "literals_only",
        "Ordinary text 'Sheet1!B2' and a string literal LEN(\"Sheet1\"); neither may change.",
        literal,
        [],
    )
    named = _base("rename_probe_name", rows(text("A1", "Totals")))
    named.defined_names.append(("Totals", "Sheet1!$B$2:$B$3"))
    add(
        "defined_name",
        "Workbook defined name Totals=Sheet1!$B$2:$B$3.",
        named,
        [
            {
                "part": "xl/workbook.xml",
                "kind": "defined_name",
                "name": "Totals",
                "formula": "'Data source'!$B$2:$B$3",
            }
        ],
    )
    validation = (
        '<dataValidations count="1"><dataValidation type="list" allowBlank="1" sqref="C1">'
        "<formula1>Sheet1!$B$2:$B$3</formula1></dataValidation></dataValidations>"
    )
    add(
        "validation",
        "Report!C1 list validation =Sheet1!$B$2:$B$3.",
        _base("rename_probe_validation", rows(text("A1", "Pick")), validations=validation),
        [
            {
                "part": "xl/worksheets/sheet2.xml",
                "kind": "validation",
                "sqref": "C1",
                "formula": "'Data source'!$B$2:$B$3",
            }
        ],
    )
    conditional = (
        f'<ext uri="{X14_CF_URI}" xmlns:x14="{X14}"><x14:conditionalFormattings>'
        f'<x14:conditionalFormatting xmlns:xm="{XM}">'
        '<x14:cfRule type="expression" priority="1" id="{6F1C2D3E-0000-4000-8000-000000000001}">'
        "<xm:f>Sheet1!$B$2&gt;5</xm:f><x14:dxf><font><b/></font></x14:dxf></x14:cfRule>"
        "<xm:sqref>D1</xm:sqref></x14:conditionalFormatting></x14:conditionalFormattings></ext>"
    )
    add(
        "conditional_x14",
        "Report!D1 cross-sheet conditional formula Sheet1!$B$2>5 in the x14 extension (xm:f).",
        _base("rename_probe_x14", rows(text("D1", "Flag")), extensions=conditional),
        [
            {
                "part": "xl/worksheets/sheet2.xml",
                "kind": "conditional",
                "sqref": "D1",
                "formula": "'Data source'!$B$2>5",
            }
        ],
        "Specification 2.1.0 §8.4 makes an 'unsupported formula extension' MANUAL_REVIEW while "
        "requiring conditional-formatting references to be rewritten. Team 9 reads the x14 "
        "conditional formula (as Excel 2010+ writes cross-sheet rules) as supported; a team "
        "reading it as an unsupported extension would classify this rename MANUAL_REVIEW.",
    )
    add(
        "hyperlink_location",
        "Report!E1 internal hyperlink location Sheet1!A1.",
        _base(
            "rename_probe_link",
            rows(text("E1", "Go to data")),
            hyperlinks='<hyperlink ref="E1" location="Sheet1!A1" display="Go to data"/>',
        ),
        [
            {
                "part": "xl/worksheets/sheet2.xml",
                "kind": "hyperlink_location",
                "ref": "E1",
                "location": "'Data source'!A1",
            }
        ],
    )
    chart = _base("rename_probe_chart", rows(text("A1", "Chart")), drawing="rId1")
    chart.add(
        "xl/worksheets/_rels/sheet2.xml.rels",
        relationships([("rId1", "drawing", "../drawings/drawing1.xml")]),
    )
    chart.add(
        "xl/drawings/drawing1.xml",
        drawing(
            anchor(
                6,
                1,
                chart_frame(
                    2,
                    "Chart 1",
                    "rId1",
                    "Values by item",
                    "Column chart of the two Sheet1 values: 10 and 6.",
                ),
            )
        ),
        "drawing",
    )
    chart.add(
        "xl/drawings/_rels/drawing1.xml.rels",
        relationships([("rId1", "chart", "../charts/chart1.xml")]),
    )
    chart.add("xl/charts/chart1.xml", bar_chart("Sheet1!$B$2:$B$3", [10, 6]), "chart")
    add(
        "chart_series",
        "Described chart whose series reads Sheet1!$B$2:$B$3.",
        chart,
        [
            {
                "part": "xl/charts/chart1.xml",
                "kind": "chart_formula",
                "index": 0,
                "formula": "'Data source'!$B$2:$B$3",
            }
        ],
    )
    return found


def expected_finding() -> dict[str, str]:
    """Team 9's reading for every variant: one supported generic-name finding (§7.3/§8.4)."""
    return {
        "ruleId": "WORKSHEET_NAME",
        "category": "WORKSHEET_NAME",
        "worksheet": "Sheet1",
        "target": TARGET,
        "classification": "REQUIRES_USER_INPUT",
    }


def plan(variant: Variant) -> dict[str, Any]:
    """The comparator effect plan for an approved rename of this variant."""
    return {
        "type": "rename",
        "sheet": "Sheet1",
        "name": NEW_NAME,
        "references": variant.references,
    }
