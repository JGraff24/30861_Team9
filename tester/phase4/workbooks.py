"""Team 9's provisional Phase 4 synthetic fixtures, written part by part as literal XML.

Every worksheet name, cell, style, drawing object, merge, hyperlink and table below is spelled
out so a reviewer can derive the six-rule expectations by reading this file and the committed
XML, never by running an engine (§15.2). The hand-written expectations live in
`tests/fixtures/expectations-phase4.json`, with the review record each fixture still needs.

Conventions that keep incidental findings predictable:
- Cell style 0 has no font color and no fill (black on white under the §7.7 display assumption).
- Populated regions are separated by empty rows/columns unless a table candidate is intended.
- Only `styles()` below defines colors; each fixture uses a style index on purpose.
"""

import hashlib
import io
import struct
import zipfile
import zlib
from collections.abc import Iterable
from xml.sax.saxutils import escape, quoteattr

from tester.suite.workbooks import png_bytes

FIXED_TIME = (2026, 10, 29, 0, 0, 0)
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
DECORATIVE_URI = "{C183D7F6-B498-43B3-948B-1728B52AA6E4}"
ADEC = "http://schemas.microsoft.com/office/drawing/2017/decorative"
CT = {
    "workbook": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    "worksheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
    "styles": "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml",
    "sst": "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml",
    "drawing": "application/vnd.openxmlformats-officedocument.drawing+xml",
    "chart": "application/vnd.openxmlformats-officedocument.drawingml.chart+xml",
    "table": "application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml",
    "comments": "application/vnd.openxmlformats-officedocument.spreadsheetml.comments+xml",
    "core": "application/vnd.openxmlformats-package.core-properties+xml",
    "app": "application/vnd.openxmlformats-officedocument.extended-properties+xml",
    "custom": "application/vnd.team9.opaque-test-part",
}
REL = {
    "worksheet": f"{R}/worksheet",
    "styles": f"{R}/styles",
    "sst": f"{R}/sharedStrings",
    "drawing": f"{R}/drawing",
    "image": f"{R}/image",
    "chart": f"{R}/chart",
    "table": f"{R}/table",
    "hyperlink": f"{R}/hyperlink",
    "comments": f"{R}/comments",
    "vml": f"{R}/vmlDrawing",
    "office": f"{R}/officeDocument",
    "core": "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties",
    "app": f"{R}/extended-properties",
    "opaque": "urn:team9:test:opaque-part",
    "theme": f"{R}/theme",
}
XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

# --- styles -----------------------------------------------------------------------------------
# fonts:  0 default (no color)   1 red FF0000   2 bold (no color)
# fills:  0 none   1 gray125 (required placeholder)   2 solid green 00B050
# cellXfs:  0 default   1 red font   2 green fill   3 bold
# dxfs:   0 bold only (no color, so conditional formatting using it is not a visual color rule)
STYLES = (
    XML_DECLARATION + f'<styleSheet xmlns="{MAIN}">'
    '<fonts count="3">'
    '<font><sz val="11"/><name val="Calibri"/></font>'
    '<font><sz val="11"/><color rgb="FFFF0000"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font>'
    "</fonts>"
    '<fills count="3"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FF00B050"/></patternFill></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="0" fontId="0" fillId="2" borderId="0" xfId="0" applyFill="1"/>'
    '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '<dxfs count="1"><dxf><font><b/></font></dxf></dxfs>'
    "</styleSheet>"
)


# --- cells ------------------------------------------------------------------------------------


def text(ref: str, value: str, style: int = 0) -> str:
    space = ' xml:space="preserve"' if value != value.strip() else ""
    styled = f' s="{style}"' if style else ""
    return f'<c r="{ref}"{styled} t="inlineStr"><is><t{space}>{escape(value)}</t></is></c>'


def number(ref: str, value: int | float, style: int = 0) -> str:
    styled = f' s="{style}"' if style else ""
    return f'<c r="{ref}"{styled}><v>{value}</v></c>'


def shared(ref: str, index: int) -> str:
    return f'<c r="{ref}" t="s"><v>{index}</v></c>'


def formula(ref: str, expression: str, cached: str | int, *, string: bool = False) -> str:
    kind = ' t="str"' if string else ""
    return f'<c r="{ref}"{kind}><f>{escape(expression)}</f><v>{escape(str(cached))}</v></c>'


def rows(*cells: str) -> str:
    """Group explicitly referenced cells into rows, in the order given."""
    grouped: dict[int, list[str]] = {}
    for cell in cells:
        ref = cell.split('r="', 1)[1].split('"', 1)[0]
        row = int("".join(ch for ch in ref if ch.isdigit()))
        grouped.setdefault(row, []).append(cell)
    return "".join(f'<row r="{row}">{"".join(grouped[row])}</row>' for row in sorted(grouped))


def worksheet(
    sheet_data: str,
    *,
    merges: Iterable[str] = (),
    conditional: str = "",
    validations: str = "",
    hyperlinks: str = "",
    drawing: str | None = None,
    legacy_drawing: str | None = None,
    tables: Iterable[str] = (),
    extensions: str = "",
) -> str:
    """CT_Worksheet children in schema order; `extensions` holds raw <ext> elements."""
    merge_list = list(merges)
    table_list = list(tables)
    body = f"<sheetData>{sheet_data}</sheetData>"
    if merge_list:
        body += f'<mergeCells count="{len(merge_list)}">'
        body += "".join(f'<mergeCell ref="{ref}"/>' for ref in merge_list) + "</mergeCells>"
    body += conditional + validations
    if hyperlinks:
        body += f"<hyperlinks>{hyperlinks}</hyperlinks>"
    body += (
        '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
    )
    if drawing:
        body += f'<drawing r:id="{drawing}"/>'
    if legacy_drawing:
        body += f'<legacyDrawing r:id="{legacy_drawing}"/>'
    if table_list:
        body += f'<tableParts count="{len(table_list)}">'
        body += "".join(f'<tablePart r:id="{rid}"/>' for rid in table_list) + "</tableParts>"
    if extensions:
        body += f"<extLst>{extensions}</extLst>"
    return XML_DECLARATION + f'<worksheet xmlns="{MAIN}" xmlns:r="{R}">{body}</worksheet>'


# --- drawings ---------------------------------------------------------------------------------


def cnvpr(
    object_id: int, name: str, title: str | None, descr: str | None, decorative: bool = False
) -> str:
    attrs = f' id="{object_id}" name={quoteattr(name)}'
    if title is not None:
        attrs += f" title={quoteattr(title)}"
    if descr is not None:
        attrs += f" descr={quoteattr(descr)}"
    if not decorative:
        return f"<xdr:cNvPr{attrs}/>"
    return (
        f'<xdr:cNvPr{attrs}><a:extLst><a:ext uri="{DECORATIVE_URI}">'
        f'<adec:decorative xmlns:adec="{ADEC}" val="1"/></a:ext></a:extLst></xdr:cNvPr>'
    )


def anchor(column: int, row: int, body: str) -> str:
    return (
        '<xdr:twoCellAnchor editAs="oneCell">'
        f"<xdr:from><xdr:col>{column}</xdr:col><xdr:colOff>0</xdr:colOff>"
        f"<xdr:row>{row}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
        f"<xdr:to><xdr:col>{column + 2}</xdr:col><xdr:colOff>0</xdr:colOff>"
        f"<xdr:row>{row + 4}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
        f"{body}<xdr:clientData/></xdr:twoCellAnchor>"
    )


XFRM = '<a:xfrm><a:off x="0" y="0"/><a:ext cx="914400" cy="914400"/></a:xfrm>'
RECT = '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
# A visible gray fill and black outline so reviewers can find shapes during the Excel check.
SHAPE_LOOK = (
    '<a:solidFill><a:srgbClr val="D9D9D9"/></a:solidFill>'
    '<a:ln><a:solidFill><a:srgbClr val="000000"/></a:solidFill></a:ln>'
)
X14_CF_URI = "{78C0D931-6437-407d-A8EE-F0AAD7539E65}"
X14 = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"
XM = "http://schemas.microsoft.com/office/excel/2006/main"


def picture(object_id: int, name: str, rel_id: str, title: str | None, descr: str | None) -> str:
    return (
        f"<xdr:pic><xdr:nvPicPr>{cnvpr(object_id, name, title, descr)}"
        '<xdr:cNvPicPr><a:picLocks noChangeAspect="1"/></xdr:cNvPicPr></xdr:nvPicPr>'
        f'<xdr:blipFill><a:blip r:embed="{rel_id}"/><a:stretch><a:fillRect/></a:stretch>'
        f"</xdr:blipFill><xdr:spPr>{XFRM}{RECT}</xdr:spPr></xdr:pic>"
    )


def shape(
    object_id: int,
    name: str,
    title: str | None,
    descr: str | None,
    *,
    decorative: bool = False,
) -> str:
    return (
        f"<xdr:sp><xdr:nvSpPr>{cnvpr(object_id, name, title, descr, decorative)}"
        f"<xdr:cNvSpPr/></xdr:nvSpPr><xdr:spPr>{XFRM}{RECT}{SHAPE_LOOK}</xdr:spPr></xdr:sp>"
    )


def chart_frame(object_id: int, name: str, rel_id: str, title: str, descr: str) -> str:
    return (
        f"<xdr:graphicFrame><xdr:nvGraphicFramePr>{cnvpr(object_id, name, title, descr)}"
        "<xdr:cNvGraphicFramePr/></xdr:nvGraphicFramePr>"
        '<xdr:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/></xdr:xfrm>'
        f'<a:graphic><a:graphicData uri="{C}"><c:chart xmlns:c="{C}" r:id="{rel_id}"/>'
        "</a:graphicData></a:graphic></xdr:graphicFrame>"
    )


def drawing(*anchors: str) -> str:
    return (
        XML_DECLARATION
        + f'<xdr:wsDr xmlns:xdr="{XDR}" xmlns:a="{A}" xmlns:r="{R}">{"".join(anchors)}</xdr:wsDr>'
    )


def bar_chart(series_formula: str, values: list[int]) -> str:
    points = "".join(f'<c:pt idx="{i}"><c:v>{v}</c:v></c:pt>' for i, v in enumerate(values))
    return (
        XML_DECLARATION + f'<c:chartSpace xmlns:c="{C}" xmlns:a="{A}" xmlns:r="{R}"><c:chart>'
        '<c:autoTitleDeleted val="1"/><c:plotArea><c:layout/><c:barChart><c:barDir val="col"/>'
        '<c:grouping val="clustered"/><c:varyColors val="0"/><c:ser><c:idx val="0"/>'
        '<c:order val="0"/>'
        f"<c:val><c:numRef><c:f>{escape(series_formula)}</c:f><c:numCache>"
        f'<c:ptCount val="{len(values)}"/>{points}</c:numCache></c:numRef></c:val></c:ser>'
        '<c:axId val="10"/><c:axId val="20"/></c:barChart>'
        '<c:catAx><c:axId val="10"/><c:scaling><c:orientation val="minMax"/></c:scaling>'
        '<c:delete val="0"/><c:axPos val="b"/><c:crossAx val="20"/><c:crosses val="autoZero"/>'
        "</c:catAx>"
        '<c:valAx><c:axId val="20"/><c:scaling><c:orientation val="minMax"/></c:scaling>'
        '<c:delete val="0"/><c:axPos val="l"/><c:crossAx val="10"/><c:crosses val="autoZero"/>'
        '<c:crossBetween val="between"/></c:valAx>'
        '</c:plotArea><c:plotVisOnly val="1"/></c:chart></c:chartSpace>'
    )


def table(
    table_id: int,
    name: str,
    ref: str,
    columns: list[str],
    *,
    header_rows: int | None = None,
    auto_filter: bool = False,
) -> str:
    header = "" if header_rows is None else f' headerRowCount="{header_rows}"'
    filtered = f'<autoFilter ref="{ref}"/>' if auto_filter else ""
    cols = "".join(f'<tableColumn id="{i}" name={quoteattr(c)}/>' for i, c in enumerate(columns, 1))
    return (
        XML_DECLARATION + f'<table xmlns="{MAIN}" id="{table_id}" name="{name}" '
        f'displayName="{name}" ref="{ref}"{header} totalsRowShown="0">{filtered}'
        f'<tableColumns count="{len(columns)}">{cols}</tableColumns>'
        '<tableStyleInfo showFirstColumn="0" showLastColumn="0" showRowStripes="0" '
        'showColumnStripes="0"/></table>'
    )


# --- package ----------------------------------------------------------------------------------


def relationships(entries: Iterable[tuple[str, str, str]], external: Iterable[str] = ()) -> str:
    externals = set(external)
    body = ""
    for rel_id, kind, target in entries:
        mode = ' TargetMode="External"' if rel_id in externals else ""
        body += f'<Relationship Id="{rel_id}" Type="{REL[kind]}" Target={quoteattr(target)}{mode}/>'
    return XML_DECLARATION + f'<Relationships xmlns="{PKG_REL}">{body}</Relationships>'


class Package:
    """Minimal deterministic SpreadsheetML package (stored entries, fixed timestamps)."""

    def __init__(self, title: str) -> None:
        self.parts: dict[str, bytes] = {}
        self.overrides: dict[str, str] = {}
        self.sheets: list[tuple[str, int, str, str | None]] = []
        self.defined_names: list[tuple[str, str]] = []
        self.extra_defaults: dict[str, str] = {}
        self.root_relationships: list[tuple[str, str, str]] = []
        # Extra workbook relationships as (kind, target); IDs follow sheets/styles/sst.
        self.workbook_relationships: list[tuple[str, str]] = []
        self.add(
            "docProps/core.xml",
            XML_DECLARATION + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/'
            'package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:title>{escape(title)}</dc:title><dc:creator>Team 9 tester (synthetic)"
            "</dc:creator><dc:language>en-US</dc:language></cp:coreProperties>",
            "core",
        )
        self.add(
            "docProps/app.xml",
            XML_DECLARATION + '<Properties xmlns="http://schemas.openxmlformats.org/'
            'officeDocument/2006/extended-properties"><Application>Microsoft Excel'
            "</Application></Properties>",
            "app",
        )
        self.add("xl/styles.xml", STYLES, "styles")

    def add(self, name: str, content: str | bytes, kind: str | None = None) -> None:
        self.parts[name] = content.encode("utf-8") if isinstance(content, str) else content
        if kind is not None:
            self.overrides["/" + name] = CT[kind]

    def sheet(self, name: str, sheet_id: int, part: str, state: str | None = None) -> None:
        self.sheets.append((name, sheet_id, part, state))

    def build(self, *, shared_strings: list[str] | None = None) -> bytes:
        workbook_rels = []
        sheets_xml = ""
        for index, (name, sheet_id, part, state) in enumerate(self.sheets, 1):
            hidden = f' state="{state}"' if state else ""
            sheets_xml += (
                f'<sheet name={quoteattr(name)} sheetId="{sheet_id}"{hidden} r:id="rId{index}"/>'
            )
            workbook_rels.append((f"rId{index}", "worksheet", part.removeprefix("xl/")))
        workbook_rels.append((f"rId{len(self.sheets) + 1}", "styles", "styles.xml"))
        if shared_strings is not None:
            workbook_rels.append((f"rId{len(self.sheets) + 2}", "sst", "sharedStrings.xml"))
            items = "".join(f"<si><t>{escape(value)}</t></si>" for value in shared_strings)
            count = len(shared_strings)
            self.add(
                "xl/sharedStrings.xml",
                XML_DECLARATION + f'<sst xmlns="{MAIN}" count="{count + 1}" '
                f'uniqueCount="{count}">{items}</sst>',
                "sst",
            )
        for kind, target in self.workbook_relationships:
            workbook_rels.append((f"rId{len(workbook_rels) + 1}", kind, target))
        names = ""
        if self.defined_names:
            names = (
                "<definedNames>"
                + "".join(
                    f"<definedName name={quoteattr(n)}>{escape(v)}</definedName>"
                    for n, v in self.defined_names
                )
                + "</definedNames>"
            )
        self.add(
            "xl/workbook.xml",
            XML_DECLARATION + f'<workbook xmlns="{MAIN}" xmlns:r="{R}">'
            '<bookViews><workbookView activeTab="0"/></bookViews>'
            f"<sheets>{sheets_xml}</sheets>{names}</workbook>",
            "workbook",
        )
        self.add("xl/_rels/workbook.xml.rels", relationships(workbook_rels))
        self.add(
            "_rels/.rels",
            relationships(
                [
                    ("rId1", "office", "xl/workbook.xml"),
                    ("rId2", "core", "docProps/core.xml"),
                    ("rId3", "app", "docProps/app.xml"),
                    *self.root_relationships,
                ]
            ),
        )
        for sheet in self.sheets:
            self.overrides["/" + sheet[2]] = CT["worksheet"]
        defaults = {
            "rels": "application/vnd.openxmlformats-package.relationships+xml",
            "xml": "application/xml",
            "png": "image/png",
            **self.extra_defaults,
        }
        types = "".join(
            f'<Default Extension="{ext}" ContentType="{ctype}"/>' for ext, ctype in defaults.items()
        ) + "".join(
            f'<Override PartName="{part}" ContentType="{ctype}"/>'
            for part, ctype in sorted(self.overrides.items())
        )
        content_types = (
            XML_DECLARATION
            + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            + types
            + "</Types>"
        )
        order = [
            "[Content_Types].xml",
            "_rels/.rels",
            "docProps/core.xml",
            "docProps/app.xml",
            "xl/workbook.xml",
            "xl/_rels/workbook.xml.rels",
            "xl/styles.xml",
        ]
        names_in_order = order + sorted(name for name in self.parts if name not in order)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_STORED) as archive:
            for name in names_in_order:
                content = (
                    content_types.encode() if name == "[Content_Types].xml" else self.parts[name]
                )
                info = zipfile.ZipInfo(name, FIXED_TIME)
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = 0o600 << 16
                archive.writestr(info, content)
        return stream.getvalue()


# --- fixtures ---------------------------------------------------------------------------------
# Each docstring lists the findings the reviewer should derive. The JSON expectations are the
# authoritative hand-written oracle; these notes help a two-member review trace them.


def clean_all_six() -> bytes:
    """No findings under all six rules. Expected AUDIT_COMPLETE.

    Budget: formal table A1:B4 (header Item/Amount matches tableColumns, headerRowCount omitted),
    a described picture (id 2), and a descriptive hyperlink label in D1. Notes: one text cell.
    Limitation facts: single-cell regions (D1, Notes!A1) are not inferred tables; missing
    fill/font color display assumption.
    """
    package = Package("clean_all_six")
    package.sheet("Budget", 1, "xl/worksheets/sheet1.xml")
    package.sheet("Notes", 2, "xl/worksheets/sheet2.xml")
    data = rows(
        text("A1", "Item"),
        text("B1", "Amount"),
        text("A2", "Rent"),
        number("B2", 1200),
        text("A3", "Travel"),
        number("B3", 300),
        text("A4", "Supplies"),
        number("B4", 200),
        text("D1", "Annual accessibility report"),
    )
    package.add(
        "xl/worksheets/sheet1.xml",
        worksheet(
            data, hyperlinks='<hyperlink ref="D1" r:id="rId3"/>', drawing="rId1", tables=["rId2"]
        ),
    )
    package.add(
        "xl/worksheets/_rels/sheet1.xml.rels",
        relationships(
            [
                ("rId1", "drawing", "../drawings/drawing1.xml"),
                ("rId2", "table", "../tables/table1.xml"),
                ("rId3", "hyperlink", "https://example.edu/accessibility/annual-report"),
            ],
            external=["rId3"],
        ),
    )
    package.add(
        "xl/tables/table1.xml",
        table(1, "Costs", "A1:B4", ["Item", "Amount"], auto_filter=True),
        "table",
    )
    package.add(
        "xl/drawings/drawing1.xml",
        drawing(
            anchor(
                5,
                1,
                picture(
                    2,
                    "Picture 1",
                    "rId1",
                    "Team logo",
                    "Team 9 logo placeholder: a plain white square.",
                ),
            )
        ),
        "drawing",
    )
    package.add(
        "xl/drawings/_rels/drawing1.xml.rels",
        relationships([("rId1", "image", "../media/image1.png")]),
    )
    package.add("xl/media/image1.png", png_bytes())
    package.add(
        "xl/worksheets/sheet2.xml",
        worksheet(rows(text("A1", "Figures are illustrative test data."))),
    )
    return package.build()


def seeded_all_six() -> bytes:
    """One sheet seeding every rule with user-input and manual items. REMEDIATION_AVAILABLE.

    Sheet1 (sheetId 1, sheet1.xml):
      ALT_TEXT  #id=2 picture without title/descr          REQUIRES_USER_INPUT
      ALT_TEXT  #id=3 shape marked decorative              MANUAL_REVIEW
      COLOR     #cell=A5 red FF0000 text (3.998:1, nonneutral) MANUAL_REVIEW
      LINK      #cell=G1 "click here"                       REQUIRES_USER_INPUT
      MERGED    #merge=D1:E1 populated title merge          MANUAL_REVIEW
      TABLE     #range=A1:B3 dense, labels Name/Score       REQUIRES_USER_INPUT
      NAME      sheet1.xml#sheetId=1 "Sheet1"              REQUIRES_USER_INPUT
    Expenses (sheetId 2): one cell. Limitations: single-cell regions; display assumption.
    """
    package = Package("seeded_all_six")
    package.sheet("Sheet1", 1, "xl/worksheets/sheet1.xml")
    package.sheet("Expenses", 2, "xl/worksheets/sheet2.xml")
    data = rows(
        text("A1", "Name"),
        text("B1", "Score"),
        text("D1", "Quarterly scores"),
        text("G1", "click here"),
        text("A2", "Ada"),
        number("B2", 9),
        text("A3", "Lin"),
        number("B3", 7),
        text("A5", "Overdue", style=1),
    )
    package.add(
        "xl/worksheets/sheet1.xml",
        worksheet(
            data, merges=["D1:E1"], hyperlinks='<hyperlink ref="G1" r:id="rId2"/>', drawing="rId1"
        ),
    )
    package.add(
        "xl/worksheets/_rels/sheet1.xml.rels",
        relationships(
            [
                ("rId1", "drawing", "../drawings/drawing1.xml"),
                ("rId2", "hyperlink", "https://example.edu/scores"),
            ],
            external=["rId2"],
        ),
    )
    package.add(
        "xl/drawings/drawing1.xml",
        drawing(
            anchor(9, 1, picture(2, "Picture 1", "rId1", None, None)),
            anchor(9, 7, shape(3, "Divider line", None, None, decorative=True)),
        ),
        "drawing",
    )
    package.add(
        "xl/drawings/_rels/drawing1.xml.rels",
        relationships([("rId1", "image", "../media/image1.png")]),
    )
    package.add("xl/media/image1.png", png_bytes())
    package.add("xl/worksheets/sheet2.xml", worksheet(rows(text("A1", "No expenses yet."))))
    return package.build()


def review_only() -> bytes:
    """Only MANUAL_REVIEW findings. Expected REVIEW_REQUIRED.

    Summary (sheetId 1): MERGED #merge=A1:C1 ("Annual summary"); COLOR #cell=A3 (green fill
    00B050, nonneutral; black-on-green contrast passes). A3 is a single cell.
    """
    package = Package("review_only")
    package.sheet("Summary", 1, "xl/worksheets/sheet1.xml")
    package.add(
        "xl/worksheets/sheet1.xml",
        worksheet(
            rows(text("A1", "Annual summary"), text("A3", "Late", style=2)), merges=["A1:C1"]
        ),
    )
    return package.build()


def mixed_decisions() -> bytes:
    """All four supported correction families plus manual items. REMEDIATION_AVAILABLE.

    Sheet1 (sheetId 1, sheet1.xml) — data; renaming it rewrites Sheet2!A1:
      ALT_TEXT  #id=2 picture titled "Regional units", no descr  REQUIRES_USER_INPUT
      COLOR     #cell=A5 red text                           MANUAL_REVIEW
      LINK      #cell=D1 "here"                             REQUIRES_USER_INPUT
      MERGED    #merge=F1:G1 populated                      MANUAL_REVIEW
      TABLE     #range=A1:B3 dense, labels Region/Units     REQUIRES_USER_INPUT
      NAME      sheet1.xml#sheetId=1                        REQUIRES_USER_INPUT
    Sheet2 (sheetId 2, sheet2.xml) — A1 =SUM(Sheet1!B2:B3):
      NAME      sheet2.xml#sheetId=2                        REQUIRES_USER_INPUT
    Notes (sheetId 3, sheet3.xml) — one text cell; its name is the duplicate-name probe.
    """
    package = Package("mixed_decisions")
    package.sheet("Sheet1", 1, "xl/worksheets/sheet1.xml")
    package.sheet("Sheet2", 2, "xl/worksheets/sheet2.xml")
    package.sheet("Notes", 3, "xl/worksheets/sheet3.xml")
    data = rows(
        text("A1", "Region"),
        text("B1", "Units"),
        text("D1", "here"),
        text("F1", "Regional units"),
        text("A2", "North"),
        number("B2", 12),
        text("A3", "South"),
        number("B3", 4),
        text("A5", "Check totals", style=1),
    )
    package.add(
        "xl/worksheets/sheet1.xml",
        worksheet(
            data, merges=["F1:G1"], hyperlinks='<hyperlink ref="D1" r:id="rId2"/>', drawing="rId1"
        ),
    )
    package.add(
        "xl/worksheets/_rels/sheet1.xml.rels",
        relationships(
            [
                ("rId1", "drawing", "../drawings/drawing1.xml"),
                ("rId2", "hyperlink", "https://example.edu/units"),
            ],
            external=["rId2"],
        ),
    )
    package.add(
        "xl/drawings/drawing1.xml",
        drawing(anchor(9, 1, picture(2, "Picture 1", "rId1", "Regional units", None))),
        "drawing",
    )
    package.add(
        "xl/drawings/_rels/drawing1.xml.rels",
        relationships([("rId1", "image", "../media/image1.png")]),
    )
    package.add("xl/media/image1.png", png_bytes())
    package.add("xl/worksheets/sheet2.xml", worksheet(rows(formula("A1", "SUM(Sheet1!B2:B3)", 16))))
    package.add("xl/worksheets/sheet3.xml", worksheet(rows(text("A1", "Units are cartons."))))
    return package.build()


def unsupported_edits() -> bytes:
    """Findings outside the safe correction profile; every APPLY must be refused.

    Sheet1 (sheetId 1): B1 =INDIRECT("Sheet1!A1") makes the rename MANUAL_REVIEW (§8.4).
      ALT_TEXT  #id=3 decorative shape                         MANUAL_REVIEW
      ALT_TEXT  #xml=/wsDr[1]/twoCellAnchor[1]/pic[1] dup id 4  MANUAL_REVIEW
      ALT_TEXT  #xml=/wsDr[1]/twoCellAnchor[2]/pic[1] dup id 4  MANUAL_REVIEW
      COLOR     #cell=A7 red text                               MANUAL_REVIEW
      LINK      #cell=D1 =HYPERLINK("https://example.edu","click here")  MANUAL_REVIEW
      MERGED    #merge=F1:G1                                    MANUAL_REVIEW
      NAME      sheet1.xml#sheetId=1 (INDIRECT present)        MANUAL_REVIEW
    Limitation facts: duplicate object IDs; formula hyperlink not modified; INDIRECT rename.
    """
    package = Package("unsupported_edits")
    package.sheet("Sheet1", 1, "xl/worksheets/sheet1.xml")
    data = rows(
        text("A1", "Lookup"),
        formula("B1", 'INDIRECT("Sheet1!A1")', "Lookup", string=True),
        formula("D1", 'HYPERLINK("https://example.edu","click here")', "click here", string=True),
        text("F1", "Merged note"),
        text("A7", "Late", style=1),
    )
    package.add("xl/worksheets/sheet1.xml", worksheet(data, merges=["F1:G1"], drawing="rId1"))
    package.add(
        "xl/worksheets/_rels/sheet1.xml.rels",
        relationships([("rId1", "drawing", "../drawings/drawing1.xml")]),
    )
    package.add(
        "xl/drawings/drawing1.xml",
        drawing(
            anchor(9, 1, picture(4, "Picture A", "rId1", None, None)),
            anchor(9, 7, picture(4, "Picture B", "rId1", None, None)),
            anchor(9, 13, shape(3, "Divider", None, None, decorative=True)),
        ),
        "drawing",
    )
    package.add(
        "xl/drawings/_rels/drawing1.xml.rels",
        relationships([("rId1", "image", "../media/image1.png")]),
    )
    package.add("xl/media/image1.png", png_bytes())
    return package.build()


def alt_supported() -> bytes:
    """Two supported ALT_TEXT targets. REMEDIATION_AVAILABLE.

    Expenses (sheetId 1): picture id 2 has a title but no descr (one finding);
    shape id 3 has neither field (one finding). Both REQUIRES_USER_INPUT.
    """
    package = Package("alt_supported")
    package.sheet("Expenses", 1, "xl/worksheets/sheet1.xml")
    package.add(
        "xl/worksheets/sheet1.xml",
        worksheet(rows(text("A1", "Monthly expenses")), drawing="rId1"),
    )
    package.add(
        "xl/worksheets/_rels/sheet1.xml.rels",
        relationships([("rId1", "drawing", "../drawings/drawing1.xml")]),
    )
    package.add(
        "xl/drawings/drawing1.xml",
        drawing(
            anchor(3, 1, picture(2, "Chart image", "rId1", "Expense chart", None)),
            anchor(3, 7, shape(3, "Callout", None, None)),
        ),
        "drawing",
    )
    package.add(
        "xl/drawings/_rels/drawing1.xml.rels",
        relationships([("rId1", "image", "../media/image1.png")]),
    )
    package.add("xl/media/image1.png", png_bytes())
    return package.build()


def link_supported() -> bytes:
    """Supported LINK_TEXT targets, including a shared string reused by another cell.

    Links (sheetId 1): A1 shared string 0 "here" with hyperlink (REQUIRES_USER_INPUT);
    A3 reuses shared string 0 without a hyperlink (no finding, must stay "here");
    C1 inline "click here" with hyperlink (REQUIRES_USER_INPUT).
    """
    package = Package("link_supported")
    package.sheet("Links", 1, "xl/worksheets/sheet1.xml")
    package.add(
        "xl/worksheets/sheet1.xml",
        worksheet(
            rows(shared("A1", 0), text("C1", "click here"), shared("A3", 0)),
            hyperlinks='<hyperlink ref="A1" r:id="rId1"/><hyperlink ref="C1" r:id="rId2"/>',
        ),
    )
    package.add(
        "xl/worksheets/_rels/sheet1.xml.rels",
        relationships(
            [
                ("rId1", "hyperlink", "https://example.edu/policy"),
                ("rId2", "hyperlink", "https://example.edu/overview"),
            ],
            external=["rId1", "rId2"],
        ),
    )
    return package.build(shared_strings=["here"])


def header_range() -> bytes:
    """One safe unstructured table candidate with a body formula. REMEDIATION_AVAILABLE.

    Scores (sheetId 1): A1:C4 dense; labels Name/Score/Grade; C2:C4 =IF(...) with string caches.
      TABLE  xl/worksheets/sheet1.xml#range=A1:C4  REQUIRES_USER_INPUT
    """
    package = Package("header_range")
    package.sheet("Scores", 1, "xl/worksheets/sheet1.xml")
    data = rows(
        text("A1", "Name"),
        text("B1", "Score"),
        text("C1", "Grade"),
        text("A2", "Ada"),
        number("B2", 9),
        formula("C2", 'IF(B2>=8,"A","B")', "A", string=True),
        text("A3", "Lin"),
        number("B3", 7),
        formula("C3", 'IF(B3>=8,"A","B")', "B", string=True),
        text("A4", "Sam"),
        number("B4", 8),
        formula("C4", 'IF(B4>=8,"A","B")', "A", string=True),
    )
    package.add("xl/worksheets/sheet1.xml", worksheet(data))
    return package.build()


def header_disabled_table() -> bytes:
    """A formal table whose header is disabled but whose first row matches its column names.

    Roster (sheetId 1): table id 1 "Roster" A1:B4 headerRowCount="0", columns Name/Team,
    row 1 cells "Name"/"Team".  TABLE xl/tables/table1.xml#tableId=1 REQUIRES_USER_INPUT.
    """
    package = Package("header_disabled_table")
    package.sheet("Roster", 1, "xl/worksheets/sheet1.xml")
    data = rows(
        text("A1", "Name"),
        text("B1", "Team"),
        text("A2", "Ada"),
        text("B2", "Blue"),
        text("A3", "Lin"),
        text("B3", "Green"),
        text("A4", "Sam"),
        text("B4", "Blue"),
    )
    package.add("xl/worksheets/sheet1.xml", worksheet(data, tables=["rId1"]))
    package.add(
        "xl/worksheets/_rels/sheet1.xml.rels",
        relationships([("rId1", "table", "../tables/table1.xml")]),
    )
    package.add(
        "xl/tables/table1.xml",
        table(1, "Roster", "A1:B4", ["Name", "Team"], header_rows=0),
        "table",
    )
    return package.build()


def rename_references() -> bytes:
    """A safe rename whose references must all be rewritten. REMEDIATION_AVAILABLE.

    Sheet1 (sheetId 1): B2=10, B3=6 (one column, no table candidate).
    Report (sheetId 2) references Sheet1 through: A1 =Sheet1!B2, A2 =SUM(Sheet1!B2:B3),
    A3 ='Sheet1'!B3, A5 =LEN("Sheet1") (string literal must not change), A6 ='Sheet1 Notes'!A1
    (similar sheet name must not change), A4 text "Sheet1!B2" (ordinary text must not change),
    C1 list validation =Sheet1!$B$2:$B$3, D1 conditional formula Sheet1!$B$2>5 (bold dxf only),
    E1 internal hyperlink location Sheet1!A1, defined name Totals=Sheet1!$B$2:$B$3, and a
    described chart whose series reads Sheet1!$B$2:$B$3. The cross-sheet conditional formula
    lives in the x14 extension (xm:f), as Excel 2010+ writes it.
    Sheet1 Notes (sheetId 3): A1 text.
      NAME  xl/worksheets/sheet1.xml#sheetId=1  REQUIRES_USER_INPUT   (only finding)
    """
    package = Package("rename_references")
    package.sheet("Sheet1", 1, "xl/worksheets/sheet1.xml")
    package.sheet("Report", 2, "xl/worksheets/sheet2.xml")
    package.sheet("Sheet1 Notes", 3, "xl/worksheets/sheet3.xml")
    package.defined_names.append(("Totals", "Sheet1!$B$2:$B$3"))
    package.add("xl/worksheets/sheet1.xml", worksheet(rows(number("B2", 10), number("B3", 6))))
    report = rows(
        formula("A1", "Sheet1!B2", 10),
        formula("A2", "SUM(Sheet1!B2:B3)", 16),
        formula("A3", "'Sheet1'!B3", 6),
        text("A4", "Sheet1!B2"),
        formula("A5", 'LEN("Sheet1")', 6),
        formula("A6", "'Sheet1 Notes'!A1", "Reviewed", string=True),
        text("E1", "Go to data"),
    )
    validation = (
        '<dataValidations count="1"><dataValidation type="list" allowBlank="1" sqref="C1">'
        "<formula1>Sheet1!$B$2:$B$3</formula1></dataValidation></dataValidations>"
    )
    # Excel 2010+ stores conditional formulas that reference another sheet in the x14
    # extension (xm:f, xm:sqref); the rename must rewrite this formula as well.
    conditional = (
        f'<ext uri="{X14_CF_URI}" xmlns:x14="{X14}"><x14:conditionalFormattings>'
        f'<x14:conditionalFormatting xmlns:xm="{XM}">'
        '<x14:cfRule type="expression" priority="1" id="{6F1C2D3E-0000-4000-8000-000000000001}">'
        "<xm:f>Sheet1!$B$2&gt;5</xm:f><x14:dxf><font><b/></font></x14:dxf></x14:cfRule>"
        "<xm:sqref>D1</xm:sqref></x14:conditionalFormatting></x14:conditionalFormattings></ext>"
    )
    package.add(
        "xl/worksheets/sheet2.xml",
        worksheet(
            report,
            extensions=conditional,
            validations=validation,
            hyperlinks='<hyperlink ref="E1" location="Sheet1!A1" display="Go to data"/>',
            drawing="rId1",
        ),
    )
    package.add(
        "xl/worksheets/_rels/sheet2.xml.rels",
        relationships([("rId1", "drawing", "../drawings/drawing1.xml")]),
    )
    package.add(
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
    package.add(
        "xl/drawings/_rels/drawing1.xml.rels",
        relationships([("rId1", "chart", "../charts/chart1.xml")]),
    )
    package.add(
        "xl/charts/chart1.xml",
        bar_chart("Sheet1!$B$2:$B$3", [10, 6]),
        "chart",
    )
    package.add("xl/worksheets/sheet3.xml", worksheet(rows(text("A1", "Reviewed"))))
    return package.build()


LARGE_PADDING = 3_000_000


def noisy_png(padding: int) -> bytes:
    """A valid 1x1 PNG whose private ancillary chunk holds incompressible deterministic bytes.

    Recompressing the package cannot shrink it below 2 MiB, so the transport limit is
    exercised however a system serializes its output ZIP.
    """
    plain = png_bytes()
    noise = hashlib.shake_256(b"team9-phase4-large-preservation").digest(padding)
    chunk = (
        struct.pack(">I", len(noise))
        + b"pdDg"
        + noise
        + struct.pack(">I", zlib.crc32(b"pdDg" + noise) & 0xFFFFFFFF)
    )
    header_end = 8 + 25  # signature + IHDR chunk
    return plain[:header_end] + chunk + plain[header_end:]


def large_preservation() -> bytes:
    """Over 2 MiB on the wire, even if recompressed (incompressible PNG chunk). One edit.

    Photos (sheetId 1): picture id 2 without alt (REQUIRES_USER_INPUT), backed by a ~3 MB PNG.
    """
    package = Package("large_preservation")
    package.sheet("Photos", 1, "xl/worksheets/sheet1.xml")
    package.add(
        "xl/worksheets/sheet1.xml", worksheet(rows(text("A1", "Site photo")), drawing="rId1")
    )
    package.add(
        "xl/worksheets/_rels/sheet1.xml.rels",
        relationships([("rId1", "drawing", "../drawings/drawing1.xml")]),
    )
    package.add(
        "xl/drawings/drawing1.xml",
        drawing(anchor(3, 1, picture(2, "Site photo", "rId1", None, None))),
        "drawing",
    )
    package.add(
        "xl/drawings/_rels/drawing1.xml.rels",
        relationships([("rId1", "image", "../media/image1.png")]),
    )
    package.add("xl/media/image1.png", noisy_png(LARGE_PADDING))
    return package.build()


def preservation_rich() -> bytes:
    """Rich package for the preservation comparator; one supported ALT edit is approved.

    Data (sheetId 1): formulas with caches (B4 =SUM(B2:B3)), styles (bold A1), a defined name,
    a list validation, a non-color conditional rule, a legacy comment (VML) on A1, picture id 2
    without alt (the approved edit), and an untouched opaque custom part.
    Hidden (sheetId 2, hidden): one text cell.
      ALT  xl/drawings/drawing1.xml#id=2              REQUIRES_USER_INPUT
      ALT  xl/drawings/vmlDrawing1.vml#id=_x0000_s1025  MANUAL_REVIEW (legacy comment shape)
    """
    package = Package("preservation_rich")
    package.sheet("Data", 1, "xl/worksheets/sheet1.xml")
    package.sheet("Hidden", 2, "xl/worksheets/sheet2.xml", state="hidden")
    package.defined_names.append(("Subtotal", "Data!$B$2:$B$3"))
    data = rows(
        text("A1", "Ledger", style=3),
        number("B2", 5),
        number("B3", 7),
        formula("B4", "SUM(B2:B3)", 12),
    )
    validation = (
        '<dataValidations count="1"><dataValidation type="whole" allowBlank="1" sqref="B2:B3">'
        "<formula1>0</formula1><formula2>100</formula2></dataValidation></dataValidations>"
    )
    conditional = (
        '<conditionalFormatting sqref="B4"><cfRule type="cellIs" dxfId="0" priority="1" '
        'operator="greaterThan"><formula>10</formula></cfRule></conditionalFormatting>'
    )
    package.add(
        "xl/worksheets/sheet1.xml",
        worksheet(
            data,
            conditional=conditional,
            validations=validation,
            drawing="rId1",
            legacy_drawing="rId3",
        ),
    )
    package.add(
        "xl/worksheets/_rels/sheet1.xml.rels",
        relationships(
            [
                ("rId1", "drawing", "../drawings/drawing1.xml"),
                ("rId2", "comments", "../comments1.xml"),
                ("rId3", "vml", "../drawings/vmlDrawing1.vml"),
            ]
        ),
    )
    package.add(
        "xl/comments1.xml",
        XML_DECLARATION + f'<comments xmlns="{MAIN}"><authors><author>Reviewer</author>'
        '</authors><commentList><comment ref="A1" authorId="0"><text><t>Check the ledger.'
        "</t></text></comment></commentList></comments>",
        "comments",
    )
    package.extra_defaults["vml"] = "application/vnd.openxmlformats-officedocument.vmlDrawing"
    package.add(
        "xl/drawings/vmlDrawing1.vml",
        '<xml xmlns:v="urn:schemas-microsoft-com:vml" '
        'xmlns:o="urn:schemas-microsoft-com:office:office" '
        'xmlns:x="urn:schemas-microsoft-com:office:excel">'
        '<o:shapelayout v:ext="edit"><o:idmap v:ext="edit" data="1"/></o:shapelayout>'
        '<v:shapetype id="_x0000_t202" coordsize="21600,21600" o:spt="202" '
        'path="m,l,21600r21600,l21600,xe"><v:stroke joinstyle="miter"/>'
        '<v:path gradientshapeok="t" o:connecttype="rect"/></v:shapetype>'
        '<v:shape id="_x0000_s1025" type="#_x0000_t202" '
        'style="position:absolute;margin-left:59.25pt;margin-top:1.5pt;width:108pt;'
        'height:59.25pt;z-index:1;visibility:hidden" fillcolor="#ffffe1" o:insetmode="auto">'
        '<v:fill color2="#ffffe1"/><v:shadow on="t" color="black" obscured="t"/>'
        '<v:path o:connecttype="none"/><v:textbox style="mso-direction-alt:auto">'
        '<div style="text-align:left"></div></v:textbox><x:ClientData ObjectType="Note">'
        "<x:MoveWithCells/><x:SizeWithCells/><x:Anchor>1, 15, 0, 2, 3, 15, 3, 16</x:Anchor>"
        "<x:AutoFill>False</x:AutoFill><x:Row>0</x:Row><x:Column>0</x:Column>"
        "</x:ClientData></v:shape></xml>",
    )
    package.add(
        "xl/drawings/drawing1.xml",
        drawing(anchor(4, 1, picture(2, "Picture 1", "rId1", None, None))),
        "drawing",
    )
    package.add(
        "xl/drawings/_rels/drawing1.xml.rels",
        relationships([("rId1", "image", "../media/image1.png")]),
    )
    package.add("xl/media/image1.png", png_bytes(64))
    package.add("xl/worksheets/sheet2.xml", worksheet(rows(text("A1", "Archived note"))))
    # Untouched opaque feature: an unknown part with its own content type and relationship.
    package.add("customXml/team9-opaque.bin", bytes(range(256)) * 4, "custom")
    package.root_relationships.append(("rId4", "opaque", "customXml/team9-opaque.bin"))
    return package.build()


def phase4_fixtures() -> dict[str, bytes]:
    """File name -> bytes for every committed Team 9 Phase 4 synthetic fixture."""
    return {
        "clean_all_six.xlsx": clean_all_six(),
        "seeded_all_six.xlsx": seeded_all_six(),
        "review_only.xlsx": review_only(),
        "mixed_decisions.xlsx": mixed_decisions(),
        "unsupported_edits.xlsx": unsupported_edits(),
        "alt_supported.xlsx": alt_supported(),
        "link_supported.xlsx": link_supported(),
        "header_range.xlsx": header_range(),
        "header_disabled_table.xlsx": header_disabled_table(),
        "rename_references.xlsx": rename_references(),
        "preservation_rich.xlsx": preservation_rich(),
        "large_preservation.xlsx": large_preservation(),
    }
