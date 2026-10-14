"""Deterministic synthetic OOXML workbooks authored field by field for independent expectations.

These are labeled synthetic fixtures, not Team 12's shared files. Every object ID, worksheet
part and alt-text field is written explicitly here so expectations never come from the engine.
"""

import io
import struct
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


def _sheet_xml(sheet: Sheet, has_drawing: bool) -> str:
    cells = []
    for ref, value in sheet.cells:
        if isinstance(value, int):
            cells.append(f'<c r="{ref}"><v>{value}</v></c>')
        else:
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>')
    drawing = '<drawing r:id="rId1"/>' if has_drawing else ""
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<worksheet xmlns="{NS["main"]}"'
        f' xmlns:r="{NS["r"]}"><sheetData><row r="1">{"".join(cells)}</row></sheetData>'
        f"{drawing}</worksheet>"
    )


def _rels(entries: list[tuple[str, str, str]]) -> bytes:
    body = "".join(
        f'<Relationship Id="{rel_id}" Type="{REL_TYPE}/{kind}" Target="{target}"/>'
        for rel_id, kind, target in entries
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Relationships xmlns="{NS["rel"]}">{body}</Relationships>'
    ).encode()


def build_workbook(sheets: list[Sheet], image_padding: int = 0) -> bytes:
    package = _Package()
    workbook_rels: list[tuple[str, str, str]] = []
    sheet_entries = []
    drawing_number = chart_number = image_number = 0
    for index, sheet in enumerate(sheets, start=1):
        rel_id = f"rId{index}"
        workbook_rels.append((rel_id, "worksheet", f"worksheets/{sheet.part}.xml"))
        state = ' state="hidden"' if sheet.hidden else ""
        sheet_entries.append(
            f'<sheet name={quoteattr(sheet.name)} sheetId="{sheet.sheet_id}"{state} '
            f'r:id="{rel_id}"/>'
        )
        part = f"xl/worksheets/{sheet.part}.xml"
        package.overrides[part] = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
        )
        package.parts[part] = _sheet_xml(sheet, bool(sheet.objects)).encode()
        if not sheet.objects:
            continue
        drawing_number += 1
        drawing_rels: dict[str, str] = {}
        anchors = "".join(
            "<xdr:twoCellAnchor><xdr:from><xdr:col>0</xdr:col><xdr:colOff>0</xdr:colOff>"
            "<xdr:row>2</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:to><xdr:col>3"
            "</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>8</xdr:row><xdr:rowOff>0</xdr:rowOff>"
            f"</xdr:to>{_object_xml(item, drawing_rels)}<xdr:clientData/></xdr:twoCellAnchor>"
            for item in sheet.objects
        )
        drawing_part = f"xl/drawings/drawing{drawing_number}.xml"
        package.parts[drawing_part] = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<xdr:wsDr '
            f'xmlns:xdr="{NS["xdr"]}" xmlns:a="{NS["a"]}" xmlns:r="{NS["r"]}">{anchors}'
            "</xdr:wsDr>"
        ).encode()
        package.overrides[drawing_part] = (
            "application/vnd.openxmlformats-officedocument.drawing+xml"
        )
        package.parts[f"xl/worksheets/_rels/{sheet.part}.xml.rels"] = _rels(
            [("rId1", "drawing", f"../drawings/drawing{drawing_number}.xml")]
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
    package.parts["xl/workbook.xml"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<workbook xmlns="{NS["main"]}"'
        f' xmlns:r="{NS["r"]}"><sheets>{"".join(sheet_entries)}</sheets></workbook>'
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
    package.parts["[Content_Types].xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns='
        '"http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default '
        'Extension="xml" ContentType="application/xml"/><Default Extension="png" '
        f'ContentType="image/png"/>{overrides}</Types>'
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
