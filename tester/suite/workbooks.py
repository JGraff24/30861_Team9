"""Deterministic synthetic OOXML workbooks authored field by field for independent expectations.

These are labeled synthetic fixtures, not Team 12's shared files. Every object ID, worksheet
part and alt-text field is written explicitly here so expectations never come from the engine.
"""

import io
import struct
import warnings
import zipfile
import zlib
from dataclasses import dataclass, field
from xml.sax.saxutils import quoteattr

FIXED_TIME = (2026, 10, 14, 0, 0, 0)
DECORATIVE_URI = "{C183D7F6-B498-43B3-948B-1728B52AA6E4}"
NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "adec": "http://schemas.microsoft.com/office/drawing/2017/decorative",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}
REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def png_bytes(padding: int = 0) -> bytes:
    """A valid 1x1 PNG; padding goes in a private ancillary chunk decoders must skip."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    # zlib stream for one white RGB pixel row, fixed so output never depends on zlib's build.
    pixels = bytes.fromhex("789c63f8ffff3f0005fe02fe")
    extra = chunk(b"pdDg", b"\x00" * padding) if padding else b""
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + extra
        + chunk(b"IDAT", pixels)
        + chunk(b"IEND", b"")
    )


@dataclass(frozen=True)
class DrawingObject:
    kind: str  # pic, sp, chart, group
    object_id: int
    name: str
    title: str | None = None
    descr: str | None = None
    decorative: bool = False
    children: tuple["DrawingObject", ...] = ()


@dataclass(frozen=True)
class Sheet:
    name: str
    sheet_id: int
    part: str  # e.g. sheet3 -> xl/worksheets/sheet3.xml
    hidden: bool = False
    objects: tuple[DrawingObject, ...] = ()
    cells: tuple[tuple[str, str | int], ...] = (("A1", "Item"), ("B1", 1))
    bulk_cells: int = 0  # replaces cells with this many numeric cells, ten per row
    protected: bool = False  # password-protected sheet structure
    protection_flag: str = "1"  # sheetProtection sheet= value; "0" stores a password inactively
    kind: str = "worksheet"  # or "chartsheet"
    rows_xml: str | None = None  # raw sheetData content, replacing cells/bulk_cells
    tail_xml: str = ""  # raw elements after drawing (legacyDrawing, mc:AlternateContent, ...)
    rels: tuple[tuple[str, str, str], ...] = ()  # extra sheet relationships (rId2 onward)
    drawing_xml: str | None = None  # raw wsDr children, used instead of objects


@dataclass
class _Package:
    parts: dict[str, bytes] = field(default_factory=dict)
    overrides: dict[str, str] = field(default_factory=dict)


def _cnvpr(item: DrawingObject) -> str:
    attrs = f' id="{item.object_id}" name={quoteattr(item.name)}'
    if item.title is not None:
        attrs += f" title={quoteattr(item.title)}"
    if item.descr is not None:
        attrs += f" descr={quoteattr(item.descr)}"
    if not item.decorative:
        return f"<xdr:cNvPr{attrs}/>"
    return (
        f'<xdr:cNvPr{attrs}><a:extLst><a:ext uri="{DECORATIVE_URI}">'
        f'<adec:decorative xmlns:adec="{NS["adec"]}" val="1"/></a:ext></a:extLst></xdr:cNvPr>'
    )


XFRM = '<a:xfrm><a:off x="0" y="0"/><a:ext cx="914400" cy="914400"/></a:xfrm>'
GEOMETRY = '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'


def _object_xml(item: DrawingObject, rels: dict[str, str]) -> str:
    if item.kind == "pic":
        rel_id = f"rId{len(rels) + 1}"
        rels[rel_id] = f"image:{item.object_id}"
        return (
            f"<xdr:pic><xdr:nvPicPr>{_cnvpr(item)}<xdr:cNvPicPr/></xdr:nvPicPr>"
            f'<xdr:blipFill><a:blip r:embed="{rel_id}"/><a:stretch><a:fillRect/></a:stretch>'
            f"</xdr:blipFill><xdr:spPr>{XFRM}{GEOMETRY}</xdr:spPr></xdr:pic>"
        )
    if item.kind == "sp":
        return (
            f"<xdr:sp><xdr:nvSpPr>{_cnvpr(item)}<xdr:cNvSpPr/></xdr:nvSpPr>"
            f"<xdr:spPr>{XFRM}{GEOMETRY}</xdr:spPr></xdr:sp>"
        )
    if item.kind == "chart":
        rel_id = f"rId{len(rels) + 1}"
        rels[rel_id] = f"chart:{item.object_id}"
        return (
            f"<xdr:graphicFrame><xdr:nvGraphicFramePr>{_cnvpr(item)}<xdr:cNvGraphicFramePr/>"
            '</xdr:nvGraphicFramePr><xdr:xfrm><a:off x="0" y="0"/><a:ext cx="914400" '
            'cy="914400"/></xdr:xfrm><a:graphic><a:graphicData '
            f'uri="{NS["c"]}"><c:chart xmlns:c="{NS["c"]}" r:id="{rel_id}"/>'
            "</a:graphicData></a:graphic></xdr:graphicFrame>"
        )
    if item.kind == "group":
        children = "".join(_object_xml(child, rels) for child in item.children)
        return (
            f"<xdr:grpSp><xdr:nvGrpSpPr>{_cnvpr(item)}<xdr:cNvGrpSpPr/></xdr:nvGrpSpPr>"
            '<xdr:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="914400" cy="914400"/>'
            '<a:chOff x="0" y="0"/><a:chExt cx="914400" cy="914400"/></a:xfrm></xdr:grpSpPr>'
            f"{children}</xdr:grpSp>"
        )
    raise ValueError(f"unknown drawing object kind {item.kind}")


CHART_XML = (
    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<c:chartSpace xmlns:c="{NS["c"]}" '
    f'xmlns:a="{NS["a"]}"><c:chart><c:plotArea><c:layout/><c:barChart><c:barDir val="col"/>'
    '<c:grouping val="clustered"/><c:ser><c:idx val="0"/><c:order val="0"/><c:val><c:numLit>'
    '<c:ptCount val="2"/><c:pt idx="0"><c:v>1</c:v></c:pt><c:pt idx="1"><c:v>2</c:v></c:pt>'
    '</c:numLit></c:val></c:ser><c:axId val="1"/><c:axId val="2"/></c:barChart><c:catAx>'
    '<c:axId val="1"/><c:scaling><c:orientation val="minMax"/></c:scaling><c:axPos val="b"/>'
    '<c:crossAx val="2"/></c:catAx><c:valAx><c:axId val="2"/><c:scaling><c:orientation '
    'val="minMax"/></c:scaling><c:axPos val="l"/><c:crossAx val="1"/></c:valAx></c:plotArea>'
    "</c:chart></c:chartSpace>"
)


# Fixed, not a real credential: only the presence of protection metadata matters here.
PROTECTION = (
    'algorithmName="SHA-512" hashValue="dGVhbTktc3ludGhldGljLXByb3RlY3Rpb24=" '
    'saltValue="c3ludGhldGljLXNhbHQ=" spinCount="100000"'
)
WORKBOOK_PROTECTION = (
    '<workbookProtection lockStructure="1" workbookAlgorithmName="SHA-512" '
    'workbookHashValue="dGVhbTktc3ludGhldGljLXByb3RlY3Rpb24=" '
    'workbookSaltValue="c3ludGhldGljLXNhbHQ=" workbookSpinCount="100000"/>'
)


def _bulk_rows(count: int) -> str:
    columns = "ABCDEFGHIJ"
    rows = []
    for start in range(0, count, len(columns)):
        row = start // len(columns) + 1
        width = min(len(columns), count - start)
        cells = "".join(f'<c r="{columns[i]}{row}"><v>1</v></c>' for i in range(width))
        rows.append(f'<row r="{row}">{cells}</row>')
    return "".join(rows)


def _sheet_xml(sheet: Sheet, has_drawing: bool) -> str:
    drawing = '<drawing r:id="rId1"/>' if has_drawing else ""
    if sheet.kind == "chartsheet":
        return (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<chartsheet '
            f'xmlns="{NS["main"]}" xmlns:r="{NS["r"]}"><sheetViews><sheetView '
            f'workbookViewId="0"/></sheetViews>{drawing}</chartsheet>'
        )
    if sheet.rows_xml is not None:
        rows = sheet.rows_xml
    elif sheet.bulk_cells:
        rows = _bulk_rows(sheet.bulk_cells)
    else:
        cells = []
        for ref, value in sheet.cells:
            if isinstance(value, int):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>')
        rows = f'<row r="1">{"".join(cells)}</row>'
    protection = (
        f"<sheetProtection {PROTECTION} sheet={quoteattr(sheet.protection_flag)}/>"
        if sheet.protected
        else ""
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<worksheet xmlns="{NS["main"]}"'
        f' xmlns:r="{NS["r"]}"><sheetData>{rows}</sheetData>'
        f"{protection}{drawing}{sheet.tail_xml}</worksheet>"
    )


def _rels(entries: list[tuple[str, str, str]]) -> bytes:
    """Relationship part; a kind that is already a full URI is used as the type verbatim."""
    body = "".join(
        f'<Relationship Id="{rel_id}" '
        f'Type="{kind if kind.startswith("http") else f"{REL_TYPE}/{kind}"}" Target="{target}"/>'
        for rel_id, kind, target in entries
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Relationships xmlns="{NS["rel"]}">{body}</Relationships>'
    ).encode()


SHEET_FOLDERS = {"worksheet": "worksheets", "chartsheet": "chartsheets"}
SHEET_CONTENT_TYPES = {
    "worksheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
    "chartsheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.chartsheet+xml",
}


@dataclass(frozen=True)
class ExtraPart:
    """A hand-written package part, optionally related from the workbook part."""

    content: bytes
    content_type: str | None = None  # Override; None falls back to the extension default
    workbook_rel: str | None = None  # relationship type (short name or full URI)


def build_workbook(
    sheets: list[Sheet],
    image_padding: int = 0,
    protect_structure: bool = False,
    extra_parts: dict[str, ExtraPart] | None = None,
    extra_defaults: dict[str, str] | None = None,
) -> bytes:
    package = _Package()
    workbook_rels: list[tuple[str, str, str]] = []
    sheet_entries = []
    drawing_number = chart_number = image_number = 0
    for index, sheet in enumerate(sheets, start=1):
        rel_id = f"rId{index}"
        folder = SHEET_FOLDERS[sheet.kind]
        workbook_rels.append((rel_id, sheet.kind, f"{folder}/{sheet.part}.xml"))
        state = ' state="hidden"' if sheet.hidden else ""
        sheet_entries.append(
            f'<sheet name={quoteattr(sheet.name)} sheetId="{sheet.sheet_id}"{state} '
            f'r:id="{rel_id}"/>'
        )
        part = f"xl/{folder}/{sheet.part}.xml"
        package.overrides[part] = SHEET_CONTENT_TYPES[sheet.kind]
        has_drawing = bool(sheet.objects) or sheet.drawing_xml is not None
        package.parts[part] = _sheet_xml(sheet, has_drawing).encode()
        sheet_rels = f"xl/{folder}/_rels/{sheet.part}.xml.rels"
        if not has_drawing:
            if sheet.rels:
                package.parts[sheet_rels] = _rels(list(sheet.rels))
            continue
        drawing_number += 1
        drawing_rels: dict[str, str] = {}
        anchors = sheet.drawing_xml or "".join(
            "<xdr:twoCellAnchor><xdr:from><xdr:col>0</xdr:col><xdr:colOff>0</xdr:colOff>"
            "<xdr:row>2</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:to><xdr:col>3"
            "</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>8</xdr:row><xdr:rowOff>0</xdr:rowOff>"
            f"</xdr:to>{_object_xml(item, drawing_rels)}<xdr:clientData/></xdr:twoCellAnchor>"
            for item in sheet.objects
        )
        drawing_part = f"xl/drawings/drawing{drawing_number}.xml"
        package.parts[drawing_part] = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<xdr:wsDr '
            f'xmlns:xdr="{NS["xdr"]}" xmlns:a="{NS["a"]}" xmlns:r="{NS["r"]}"'
            f"{f' xmlns:mc={quoteattr(NS["mc"])}' if sheet.drawing_xml else ''}>{anchors}"
            "</xdr:wsDr>"
        ).encode()
        package.overrides[drawing_part] = (
            "application/vnd.openxmlformats-officedocument.drawing+xml"
        )
        package.parts[sheet_rels] = _rels(
            [("rId1", "drawing", f"../drawings/drawing{drawing_number}.xml"), *sheet.rels]
        )
        entries = []
        for rel_id, ref in drawing_rels.items():
            if ref.startswith("image:"):
                image_number += 1
                name = f"image{image_number}.png"
                package.parts[f"xl/media/{name}"] = png_bytes(image_padding)
                image_padding = 0  # pad only the first image
                entries.append((rel_id, "image", f"../media/{name}"))
            else:
                chart_number += 1
                chart_part = f"xl/charts/chart{chart_number}.xml"
                package.parts[chart_part] = CHART_XML.encode()
                package.overrides[chart_part] = (
                    "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
                )
                entries.append((rel_id, "chart", f"../charts/chart{chart_number}.xml"))
        package.parts[f"xl/drawings/_rels/drawing{drawing_number}.xml.rels"] = _rels(entries)

    workbook_rels.append((f"rId{len(sheets) + 1}", "styles", "styles.xml"))
    for name, extra in (extra_parts or {}).items():
        package.parts[name] = extra.content
        if extra.content_type is not None:
            package.overrides[name] = extra.content_type
        if extra.workbook_rel is not None:
            target = name.removeprefix("xl/")
            workbook_rels.append((f"rId{len(workbook_rels) + 1}", extra.workbook_rel, target))
    workbook_protection = WORKBOOK_PROTECTION if protect_structure else ""
    package.parts["xl/workbook.xml"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<workbook xmlns="{NS["main"]}"'
        f' xmlns:r="{NS["r"]}">{workbook_protection}<sheets>{"".join(sheet_entries)}</sheets>'
        "</workbook>"
    ).encode()
    package.parts["xl/_rels/workbook.xml.rels"] = _rels(workbook_rels)
    package.parts["xl/styles.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<styleSheet xmlns="{NS["main"]}">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts><fills count="1">'
        '<fill><patternFill patternType="none"/></fill></fills><borders count="1"><border><left/>'
        '<right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf '
        'numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="1"><xf '
        'numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs><cellStyles '
        'count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
    ).encode()
    package.overrides["xl/workbook.xml"] = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
    )
    package.overrides["xl/styles.xml"] = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"
    )
    package.parts["_rels/.rels"] = _rels([("rId1", "officeDocument", "xl/workbook.xml")])
    overrides = "".join(
        f'<Override PartName="/{part}" ContentType="{kind}"/>'
        for part, kind in sorted(package.overrides.items())
    )
    defaults = "".join(
        f'<Default Extension="{extension}" ContentType="{kind}"/>'
        for extension, kind in sorted((extra_defaults or {}).items())
    )
    package.parts["[Content_Types].xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns='
        '"http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default '
        'Extension="xml" ContentType="application/xml"/><Default Extension="png" '
        f'ContentType="image/png"/>{defaults}{overrides}</Types>'
    ).encode()

    buffer = io.BytesIO()
    order = ["[Content_Types].xml", "_rels/.rels"] + sorted(
        name for name in package.parts if name not in {"[Content_Types].xml", "_rels/.rels"}
    )
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in order:
            info = zipfile.ZipInfo(name, FIXED_TIME)
            # Stored, not deflated: zlib builds differ across platforms, and padded boundary
            # files need an exact, predictable size.
            info.compress_type = zipfile.ZIP_STORED
            # ZipInfo defaults "version made by" to the host OS; pin it for identical bytes.
            info.create_system = 0
            archive.writestr(info, package.parts[name])
    return buffer.getvalue()


def described(kind: str, object_id: int, name: str) -> DrawingObject:
    return DrawingObject(kind, object_id, name, title=f"{name} title", descr=f"{name} description")


CLEAN_SHEETS = [
    Sheet(
        "Budget",
        1,
        "sheet1",
        objects=(
            described("pic", 2, "Logo picture"),
            described("sp", 3, "Callout shape"),
            described("chart", 4, "Spending chart"),
        ),
        cells=(("A1", "Category"), ("B1", "Amount"), ("C1", 0)),
    ),
    Sheet("Notes", 2, "sheet2", cells=(("A1", "Prepared for testing"),)),
]

SEEDED_SHEETS = [
    Sheet(
        "Sheet1",
        1,
        "sheet1",
        objects=(
            DrawingObject("pic", 2, "Picture 1", title="Logo"),
            DrawingObject("sp", 3, "Rectangle 2", title=" \t ", descr="Box"),
            DrawingObject("chart", 4, "Chart 3", decorative=True),
            DrawingObject(
                "group",
                5,
                "Group 4",
                title="Group",
                descr="Grouped items",
                children=(DrawingObject("pic", 6, "Picture 5", title="Child", descr="  "),),
            ),
        ),
    ),
    Sheet("Expenses", 2, "sheet2"),
    Sheet("sheet2", 3, "sheet3", hidden=True),
    Sheet("Sheet0", 4, "sheet4"),
]

CORRUPT_BYTES = b"PK\x03\x04" + b"\x14\x00\x00\x00" + b"This is not a readable workbook." * 8


def clean_workbook() -> bytes:
    return build_workbook(CLEAN_SHEETS)


def seeded_workbook() -> bytes:
    return build_workbook(SEEDED_SHEETS)


def padded_clean_workbook(size: int) -> bytes:
    """A valid clean workbook whose package is exactly ``size`` bytes."""
    base = len(build_workbook(CLEAN_SHEETS, image_padding=1))
    data = build_workbook(CLEAN_SHEETS, image_padding=1 + size - base)
    if len(data) != size:
        raise ValueError("padded workbook size did not converge")
    return data


def protected_workbook() -> bytes:
    """A valid package whose workbook structure and only sheet are password protected."""
    return build_workbook([Sheet("Budget", 1, "sheet1", protected=True)], protect_structure=True)


def worksheets_workbook(count: int, *, chartsheet: bool = False) -> bytes:
    """A valid package with ``count`` descriptively named worksheets, one cell each.

    With ``chartsheet``, one described chartsheet follows; it does not count as a worksheet.
    """
    sheets = [
        Sheet(f"Region {n}", n, f"sheet{n}", cells=(("A1", "Total"),)) for n in range(1, count + 1)
    ]
    if chartsheet:
        sheets.append(
            Sheet(
                "Regional trend",
                count + 1,
                "sheet1",
                kind="chartsheet",
                objects=(described("chart", 2, "Regional trend chart"),),
            )
        )
    return build_workbook(sheets)


def cells_workbook(count: int) -> bytes:
    """A valid one-sheet package with exactly ``count`` nonempty numeric cells."""
    return build_workbook([Sheet("Data", 1, "sheet1", bulk_cells=count)])


def edge_workbooks() -> dict[str, bytes]:
    """Additional valid packages; expectations are authored independently in JSON."""
    whitespace = (
        "\t\n\r \u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004"
        "\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
    )
    return {
        "unicode.xlsx": build_workbook(
            [
                Sheet(
                    " Sheet01 ",
                    1,
                    "sheet1",
                    objects=(
                        DrawingObject(
                            "pic", 7, "Whitespace title", title=whitespace, descr="Meaning"
                        ),
                        DrawingObject(
                            "sp", 8, "Whitespace description", title="Meaning", descr=whitespace
                        ),
                        DrawingObject(
                            "sp", 9, "Zero width is not White_Space", title="\u200b", descr="\u200b"
                        ),
                    ),
                ),
                Sheet("Sheet0", 2, "sheet2"),
            ]
        ),
        "repeated_ids.xlsx": build_workbook(
            [
                Sheet(
                    "Expenses",
                    1,
                    "sheet1",
                    objects=(
                        DrawingObject("pic", 7, "First duplicate"),
                        DrawingObject("pic", 7, "Second duplicate"),
                    ),
                ),
            ]
        ),
        "multiple_drawings.xlsx": build_workbook(
            [
                Sheet(
                    "Expenses", 1, "sheet1", objects=(DrawingObject("pic", 7, "Visible picture"),)
                ),
                Sheet(
                    "Hidden costs",
                    2,
                    "sheet2",
                    hidden=True,
                    objects=(DrawingObject("chart", 8, "Hidden chart"),),
                ),
            ]
        ),
    }


RICHDATA = "http://schemas.microsoft.com/office/spreadsheetml/2017/richdata"
RICHDATA2 = "http://schemas.microsoft.com/office/spreadsheetml/2017/richdata2"
RICHVALUEREL = "http://schemas.microsoft.com/office/spreadsheetml/2022/richvaluerel"
XML_HEADER = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


def _inline(ref: str, text: str) -> str:
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


def in_cell_picture_workbook() -> bytes:
    """Excel "Place in Cell" picture: a vm cell backed by metadata and xl/richData parts.

    Also carries a WPS-style xl/cellimages.xml part. Only the cell and the package-level
    parts are cell-image representations; xl/metadata.xml is not.
    """
    rows = (
        f'<row r="1">{_inline("A1", "Product")}{_inline("B1", "Photo")}</row>'
        f'<row r="2">{_inline("A2", "Desk lamp")}<c r="B2" t="e" vm="1"><v>#VALUE!</v></c></row>'
    )
    metadata = (
        f'{XML_HEADER}<metadata xmlns="{NS["main"]}" xmlns:xlrd="{RICHDATA}"><metadataTypes '
        'count="1"><metadataType name="XLRICHVALUE" minSupportedVersion="120000" copy="1" '
        'pasteAll="1" pasteValues="1" merge="1" splitFirst="1" rowColShift="1" clearFormats="1" '
        'clearComments="1" assign="1" coerce="1"/></metadataTypes><futureMetadata '
        'name="XLRICHVALUE" count="1"><bk><extLst>'
        '<ext uri="{3e2802c4-a4d2-4d8b-9148-e3be6c30e623}">'
        '<xlrd:rvb i="0"/></ext></extLst></bk></futureMetadata><valueMetadata count="1"><bk>'
        '<rc t="1" v="0"/></bk></valueMetadata></metadata>'
    )
    office = "http://schemas.microsoft.com/office"
    parts = {
        "xl/metadata.xml": ExtraPart(
            metadata.encode(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheetMetadata+xml",
            "sheetMetadata",
        ),
        "xl/richData/rdrichvalue.xml": ExtraPart(
            f'{XML_HEADER}<rvData xmlns="{RICHDATA}" count="1"><rv s="0"><v>0</v><v>5</v></rv>'
            "</rvData>".encode(),
            "application/vnd.ms-excel.rdrichvalue+xml",
            f"{office}/2017/06/relationships/rdRichValue",
        ),
        "xl/richData/rdrichvaluestructure.xml": ExtraPart(
            f'{XML_HEADER}<rvStructures xmlns="{RICHDATA}" count="1"><s t="_localImage"><k '
            'n="_rvRel:LocalImageIdentifier" t="i"/><k n="CalcOrigin" t="i"/></s></rvStructures>'
            "".encode(),
            "application/vnd.ms-excel.rdrichvaluestructure+xml",
            f"{office}/2017/06/relationships/rdRichValueStructure",
        ),
        "xl/richData/rdRichValueTypes.xml": ExtraPart(
            f'{XML_HEADER}<rvTypesInfo xmlns="{RICHDATA2}"><global><keyFlags><key name="_Self">'
            '<flag name="ExcludeFromFile" value="1"/></key></keyFlags></global></rvTypesInfo>'
            "".encode(),
            "application/vnd.ms-excel.rdrichvaluetypes+xml",
            f"{office}/2017/06/relationships/rdRichValueTypes",
        ),
        "xl/richData/richValueRel.xml": ExtraPart(
            f'{XML_HEADER}<richValueRels xmlns="{RICHVALUEREL}" xmlns:r="{NS["r"]}">'
            '<rel r:id="rId1"/></richValueRels>'.encode(),
            "application/vnd.ms-excel.richvaluerel+xml",
            f"{office}/2022/10/relationships/richValueRel",
        ),
        "xl/richData/_rels/richValueRel.xml.rels": ExtraPart(
            _rels([("rId1", "image", "../media/image1.png")])
        ),
        "xl/media/image1.png": ExtraPart(png_bytes()),
        "xl/cellimages.xml": ExtraPart(
            f'{XML_HEADER}<etc:cellImages xmlns:etc="http://www.wps.cn/officeDocument/2017/'
            f'etCustomData" xmlns:xdr="{NS["xdr"]}" xmlns:a="{NS["a"]}"><etc:cellImage><xdr:pic>'
            '<xdr:nvPicPr><xdr:cNvPr id="2" name="ID_LAMP" descr="Desk lamp photo"/>'
            "<xdr:cNvPicPr/></xdr:nvPicPr><xdr:blipFill><a:blip/></xdr:blipFill><xdr:spPr/>"
            "</xdr:pic></etc:cellImage></etc:cellImages>".encode(),
            None,
            "http://www.wps.cn/officeDocument/2020/cellImage",
        ),
    }
    return build_workbook([Sheet("Product photos", 1, "sheet1", rows_xml=rows)], extra_parts=parts)


def image_formula_workbook() -> bytes:
    """An IMAGE() formula cell beside an ordinary formula; no value metadata, no rich data."""
    rows = (
        f'<row r="1">{_inline("A1", "Brand")}{_inline("B1", "Logo")}<c r="C1"><f>SUM(1,2)</f>'
        "<v>3</v></c></row>"
        f'<row r="2">{_inline("A2", "Team 9")}<c r="B2" t="e"><f>'
        '_xlfn.IMAGE("https://example.invalid/logo.png","Team 9 logo")</f><v>#VALUE!</v></c>'
        "</row>"
    )
    return build_workbook([Sheet("Brand logos", 1, "sheet1", rows_xml=rows)])


def _anchor(body: str) -> str:
    return (
        "<xdr:twoCellAnchor><xdr:from><xdr:col>0</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>2"
        "</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:to><xdr:col>3</xdr:col><xdr:colOff>0"
        "</xdr:colOff><xdr:row>8</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
        f"{body}<xdr:clientData/></xdr:twoCellAnchor>"
    )


def _shape(object_id: int, name: str, title: str | None = None, descr: str | None = None) -> str:
    return _object_xml(DrawingObject("sp", object_id, name, title=title, descr=descr), {})


def form_controls_workbook() -> bytes:
    """Form control and OLE object nested in mc:AlternateContent, plus drawing-level
    alternate content and an undescribed group around a described shape."""
    mc = f'xmlns:mc="{NS["mc"]}"'
    tail = (
        f'<mc:AlternateContent {mc}><mc:Choice Requires="x14"><oleObjects><mc:AlternateContent>'
        '<mc:Choice Requires="x14"><oleObject progId="Package" shapeId="1026" r:id="rId3">'
        '<objectPr defaultSize="0" autoPict="0"><anchor moveWithCells="1"><from><xdr:col '
        f'xmlns:xdr="{NS["xdr"]}">4</xdr:col></from></anchor></objectPr></oleObject></mc:Choice>'
        '<mc:Fallback><oleObject progId="Package" shapeId="1026" r:id="rId3"/></mc:Fallback>'
        "</mc:AlternateContent></oleObjects></mc:Choice></mc:AlternateContent>"
        f'<mc:AlternateContent {mc}><mc:Choice Requires="x14"><controls><mc:AlternateContent>'
        '<mc:Choice Requires="x14"><control shapeId="1025" r:id="rId2" name="Check Box 1">'
        '<controlPr defaultSize="0" autoFill="0" autoLine="0" autoPict="0"/></control>'
        "</mc:Choice></mc:AlternateContent></controls></mc:Choice></mc:AlternateContent>"
    )
    drawing = (
        _anchor(
            '<mc:AlternateContent><mc:Choice Requires="a14">'
            + _shape(2, "Ink sketch", "Sketch", "Hand-drawn arrow")
            + "</mc:Choice><mc:Fallback>"
            + _shape(3, "Ink fallback")
            + "</mc:Fallback></mc:AlternateContent>"
        )
        + '<mc:AlternateContent><mc:Choice Requires="a14">'
        + _anchor(_shape(4, "Check Box 1"))
        + "</mc:Choice></mc:AlternateContent>"
        + _anchor(
            '<xdr:grpSp><xdr:nvGrpSpPr><xdr:cNvPr id="5" name="Group 5"/><xdr:cNvGrpSpPr/>'
            '</xdr:nvGrpSpPr><xdr:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="914400" '
            'cy="914400"/><a:chOff x="0" y="0"/><a:chExt cx="914400" cy="914400"/></a:xfrm>'
            "</xdr:grpSpPr>"
            + _shape(6, "Legend box", "Legend", "Colour key for the survey answers")
            + "</xdr:grpSp>"
        )
    )
    sheet = Sheet(
        "Survey",
        1,
        "sheet1",
        cells=(("A1", "Question"), ("B1", "Agree")),
        tail_xml=tail,
        drawing_xml=drawing,
        rels=(
            ("rId2", "ctrlProp", "../ctrlProps/ctrlProp1.xml"),
            ("rId3", "oleObject", "../embeddings/oleObject1.bin"),
        ),
    )
    return build_workbook(
        [sheet],
        extra_parts={
            "xl/ctrlProps/ctrlProp1.xml": ExtraPart(
                f'{XML_HEADER}<formControlPr xmlns="http://schemas.microsoft.com/office/'
                'spreadsheetml/2009/9/main" objectType="CheckBox" lockText="1"/>'.encode(),
                "application/vnd.ms-excel.controlproperties+xml",
            ),
            "xl/embeddings/oleObject1.bin": ExtraPart(
                b"synthetic OLE payload marker; never parsed",
                "application/vnd.openxmlformats-officedocument.oleObject",
            ),
        },
    )


def vml_comments_workbook() -> bytes:
    """Two cell comments whose notes are legacy VML shapes, plus a shapetype that is not a shape."""
    vml = (
        '<xml xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:'
        'office" xmlns:x="urn:schemas-microsoft-com:office:excel"><o:shapelayout v:ext="edit">'
        '<o:idmap v:ext="edit" data="1"/></o:shapelayout><v:shapetype id="_x0000_t202" '
        'coordsize="21600,21600" o:spt="202" path="m,l,21600r21600,l21600,xe"><v:stroke '
        'joinstyle="miter"/><v:path gradientshapeok="t" o:connecttype="rect"/></v:shapetype>'
        + "".join(
            f'<v:shape id="_x0000_s{1025 + n}" type="#_x0000_t202" style="position:absolute;'
            f'visibility:hidden" fillcolor="#ffffe1"><v:textbox/><x:ClientData ObjectType="Note">'
            f"<x:MoveWithCells/><x:Row>{n}</x:Row><x:Column>1</x:Column></x:ClientData></v:shape>"
            for n in range(2)
        )
        + "</xml>"
    )
    comments = (
        f'{XML_HEADER}<comments xmlns="{NS["main"]}"><authors><author>Reviewer</author></authors>'
        '<commentList><comment ref="B1" authorId="0"><text><t>Check the total.</t></text></comment>'
        '<comment ref="B2" authorId="0"><text><t>Source: survey.</t></text></comment>'
        "</commentList></comments>"
    )
    sheet = Sheet(
        "Review notes",
        1,
        "sheet1",
        rows_xml=(
            f'<row r="1">{_inline("A1", "Total")}<c r="B1"><v>42</v></c></row>'
            f'<row r="2">{_inline("A2", "Source")}{_inline("B2", "Survey")}</row>'
        ),
        tail_xml='<legacyDrawing r:id="rId2"/>',
        rels=(
            ("rId2", "vmlDrawing", "../drawings/vmlDrawing1.vml"),
            ("rId3", "comments", "../comments1.xml"),
        ),
    )
    return build_workbook(
        [sheet],
        extra_parts={
            "xl/drawings/vmlDrawing1.vml": ExtraPart(vml.encode()),
            "xl/comments1.xml": ExtraPart(
                comments.encode(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.comments+xml",
            ),
        },
        extra_defaults={"vml": "application/vnd.openxmlformats-officedocument.vmlDrawing"},
    )


def inactive_protection_workbook() -> bytes:
    """A stored sheet password hash with sheet="0": protection is not active."""
    return build_workbook([Sheet("Budget", 1, "sheet1", protected=True, protection_flag="0")])


def pod_contract_workbooks() -> dict[str, bytes]:
    """Pod shared API contract 1.1 representations; expectations are authored in JSON."""
    return {
        "in_cell_picture.xlsx": in_cell_picture_workbook(),
        "image_formula.xlsx": image_formula_workbook(),
        "form_controls.xlsx": form_controls_workbook(),
        "vml_comments.xlsx": vml_comments_workbook(),
        "inactive_protection.xlsx": inactive_protection_workbook(),
    }


def hostile_workbooks() -> dict[str, bytes]:
    """Deterministic malformed packages. These never contact relationships or execute XML."""
    with zipfile.ZipFile(io.BytesIO(clean_workbook())) as archive:
        original = [(info.filename, archive.read(info)) for info in archive.infolist()]

    def package(parts: list[tuple[str, bytes]], *, encrypted: bool = False) -> bytes:
        # ZIP_STORED and fixed headers avoid host/zlib-dependent fixture bytes.
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for name, content in parts:
                info = zipfile.ZipInfo(name, FIXED_TIME)
                info.create_system = 0
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore", message="Duplicate name:", category=UserWarning
                    )
                    archive.writestr(info, content)
        data = output.getvalue()
        if not encrypted:
            return data
        return encrypted_zip(parts)

    dtd = []
    for name, content in original:
        if name == "xl/workbook.xml":
            content = content.replace(
                b"<workbook ", b'<!DOCTYPE workbook [<!ENTITY probe "unsafe">]><workbook ', 1
            )
        dtd.append((name, content))
    return {
        "zip_traversal.xlsx": package(original + [("../escape.xml", b"unsafe")]),
        "zip_duplicate.xlsx": package(
            original + [("xl/workbook.xml", dict(original)["xl/workbook.xml"])]
        ),
        "xml_dtd.xlsx": package(dtd),
        "macro_parts.xlsx": package(
            original + [("xl/vbaProject.bin", b"synthetic macro marker; never execute")]
        ),
        "encrypted_zip.xlsx": package(original, encrypted=True),
        "protected_sheet.xlsx": build_workbook([Sheet("Budget", 1, "sheet1", protected=True)]),
        "protected_structure.xlsx": build_workbook(
            [Sheet("Budget", 1, "sheet1")], protect_structure=True
        ),
    }


ZIP_FIXTURE_PASSWORD = b"fixture-only-password"


def encrypted_zip(parts: list[tuple[str, bytes]]) -> bytes:
    """A genuinely password-encrypted ZIP package with deterministic ZipCrypto headers.

    This exercises encrypted ZIP rejection, rather than Office's separate CFB encryption.
    The fixed password and nonrandom headers are for this synthetic fixture only.
    """

    def crc_byte(value: int, crc: int) -> int:
        value ^= crc & 255
        for _ in range(8):
            value = (value >> 1) ^ (0xEDB88320 if value & 1 else 0)
        return (crc >> 8) ^ value

    def encrypt(data: bytes) -> bytes:
        keys = [0x12345678, 0x23456789, 0x34567890]

        def update(value: int) -> None:
            keys[0] = crc_byte(value, keys[0])
            keys[1] = ((keys[1] + (keys[0] & 255)) * 134775813 + 1) & 0xFFFFFFFF
            keys[2] = crc_byte(keys[1] >> 24, keys[2])

        for value in ZIP_FIXTURE_PASSWORD:
            update(value)
        result = bytearray()
        for value in data:
            key = keys[2] | 2
            result.append(value ^ (((key * (key ^ 1)) >> 8) & 255))
            update(value)
        return bytes(result)

    output = bytearray()
    directory = bytearray()
    dos_date = ((FIXED_TIME[0] - 1980) << 9) | (FIXED_TIME[1] << 5) | FIXED_TIME[2]
    for name, data in parts:
        filename = name.encode("utf-8")
        checksum = zlib.crc32(data) & 0xFFFFFFFF
        encrypted = encrypt(bytes(range(11)) + bytes([checksum >> 24]) + data)
        offset = len(output)
        output.extend(
            struct.pack(
                "<IHHHHHIIIHH",
                0x04034B50,
                20,
                1,
                0,
                0,
                dos_date,
                checksum,
                len(encrypted),
                len(data),
                len(filename),
                0,
            )
        )
        output.extend(filename)
        output.extend(encrypted)
        directory.extend(
            struct.pack(
                "<IHHHHHHIIIHHHHHII",
                0x02014B50,
                20,
                20,
                1,
                0,
                0,
                dos_date,
                checksum,
                len(encrypted),
                len(data),
                len(filename),
                0,
                0,
                0,
                0,
                0,
                offset,
            )
        )
        directory.extend(filename)
    offset = len(output)
    output.extend(directory)
    output.extend(
        struct.pack(
            "<IHHHHIIH", 0x06054B50, 0, 0, len(parts), len(parts), len(directory), offset, 0
        )
    )
    return bytes(output)
