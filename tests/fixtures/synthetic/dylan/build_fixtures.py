"""Rebuild Dylan's synthetic workbook fixtures (NOT Team 12's shared fixtures).

Usage from the repository root:  uv run python tests/fixtures/synthetic/dylan/build_fixtures.py

Every seed below is deliberate; expectations.json was written by hand from these seeds.
Rebuilding may change ZIP bytes on another zlib, which changes the recorded SHA256 and
therefore every expected findingId; update expectations.json sha256 values if so.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "unit"))

from ooxml_builder import (  # noqa: E402
    Drawing,
    Sheet,
    Workbook,
    alternate,
    anchor,
    chart,
    cnvpr,
    connector,
    drawing_xml,
    group,
    pic,
    shape,
)

CELLS = (
    '<row r="1"><c r="A1" t="inlineStr"><is><t>Quarter</t></is></c>'
    '<c r="B1" t="inlineStr"><is><t>Total</t></is></c></row>'
    '<row r="2"><c r="A2"><v>1</v></c><c r="B2"><v>0</v></c></row>'
    '<row r="3"><c r="A3"><v>2</v></c><c r="B3"><f>B2+10</f><v>10</v></c></row>'
)


def clean() -> Workbook:
    d1 = drawing_xml(
        anchor(pic(cnvpr("2", "Logo", title="Company logo", descr="Blue square company logo"))),
        anchor(chart(cnvpr("3", "Totals chart", title="Totals", descr="Totals rise from 0 to 10"))),
        anchor(shape(cnvpr("4", "Note", title="Note", descr="Totals exclude tax"), "Note")),
    )
    return Workbook(
        [
            Sheet(
                "Budget",
                "xl/worksheets/sheet1.xml",
                "1",
                cells=CELLS,
                drawing=Drawing("xl/drawings/drawing1.xml", d1),
            ),
            Sheet("Lookup", "xl/worksheets/sheet2.xml", "2", cells=CELLS, state="hidden"),
        ]
    )


def seeded() -> Workbook:
    d1 = drawing_xml(
        anchor(pic(cnvpr("2", "Picture 1"))),  # missing both
        anchor(chart(cnvpr("3", "Chart 1", title="Revenue"))),  # missing description
        anchor(shape(cnvpr("4", "Rectangle 1", title=" ", descr="\u00a0\u3000"), "Visible")),
        anchor(pic(cnvpr("5", "Divider", title="Line", descr="Line", decorative="1"))),
        anchor(
            group(
                cnvpr("6", "Group 1", title="Org chart", descr="Three teams"),
                shape(cnvpr("7", "Team A", title="Team A")),  # child missing description
                connector(cnvpr("8", "Link", title="Link", descr="Connects teams")),
            )
        ),
    )
    d2 = drawing_xml(anchor(pic(cnvpr("2", "Picture 9", descr=""))))  # empty descr, no title
    return Workbook(
        [
            Sheet(
                "Sheet1",
                "xl/worksheets/sheet1.xml",
                "1",
                cells=CELLS,
                drawing=Drawing("xl/drawings/drawing1.xml", d1),
            ),
            Sheet(
                "sheet02",
                "xl/worksheets/sheet2.xml",
                "2",
                state="hidden",
                drawing=Drawing("xl/drawings/drawing2.xml", d2, chart_part="xl/charts/chart2.xml"),
            ),
            Sheet("Expenses", "xl/worksheets/sheet3.xml", "3", cells=CELLS),
        ]
    )


VML = (
    '<xml xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office"'
    ' xmlns:x="urn:schemas-microsoft-com:office:excel">'
    '<v:shapetype id="_x0000_t202" coordsize="21600,21600" o:spt="202"/>'
    '<v:shape id="_x0000_s1025" type="#_x0000_t202"><x:ClientData ObjectType="Note">'
    "<x:Row>0</x:Row><x:Column>0</x:Column></x:ClientData></v:shape>"
    '<v:shape id="_x0000_s1026"><x:ClientData ObjectType="Button"/></v:shape>'
    "</xml>"
)


def edge_cases() -> Workbook:
    d1 = drawing_xml(
        anchor(pic(cnvpr("4", "Dup A"))),
        anchor(pic(cnvpr("4", "Dup B", title="t", descr="d"))),
        anchor(shape(cnvpr(None, "No id"))),
        anchor(
            alternate(
                chart(cnvpr("9", "Slicer", title="Region", descr="Filters region")),
                shape(cnvpr("9", "Fallback")),
            )
        ),
    )
    return Workbook(
        [
            Sheet(
                "Summary",
                "xl/worksheets/sheet1.xml",
                "1",
                cells=CELLS,
                drawing=Drawing("xl/drawings/drawing1.xml", d1),
                vml_part="xl/drawings/vmlDrawing1.vml",
                vml_xml=VML,
            ),
        ]
    )


def main() -> None:
    outputs = {
        "clean.xlsx": clean().build(),
        "seeded_issues.xlsx": seeded().build(),
        "edge_cases.xlsx": edge_cases().build(),
    }
    clean_bytes = outputs["clean.xlsx"]
    outputs["corrupt.xlsx"] = clean_bytes[: len(clean_bytes) // 2]  # truncated: unreadable
    for name, data in outputs.items():
        (HERE / name).write_bytes(data)
        print(name, len(data))


if __name__ == "__main__":
    main()
