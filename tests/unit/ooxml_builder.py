"""Synthetic OOXML workbook packages for Dylan's engine tests (test helper, not production).

Every object ID, name, title, description and decorative marker is chosen explicitly by
the caller, so expected findings are authored from the seeds, never from audit() output.
"""

import struct
import zipfile
from dataclasses import dataclass, field
from xml.sax.saxutils import quoteattr

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
ADEC = "http://schemas.microsoft.com/office/drawing/2017/decorative"
DECORATIVE_EXT_URI = "{C183D7F6-B498-43B3-948B-1728B52AA6E4}"
CT_WORKBOOK = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
CT_WORKSHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
CT_CHARTSHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml.chartsheet+xml"
CT_DRAWING = "application/vnd.openxmlformats-officedocument.drawing+xml"
CT_CHART = "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
CT_VML = "application/vnd.openxmlformats-officedocument.vmlDrawing"
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
# A valid 1x1 transparent PNG.
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


def attr(name: str, value: str | None) -> str:
    return "" if value is None else f" {name}={quoteattr(value)}"


def cnvpr(
    object_id: str | None,
    name: str | None,
    title: str | None = None,
    descr: str | None = None,
    decorative: str | None = None,
    *,
    decorative_attr: bool = True,
) -> str:
    """xdr:cNvPr with optional native decorative extension (val omitted if not decorative_attr)."""
    attrs = attr("id", object_id) + attr("name", name) + attr("descr", descr) + attr("title", title)
    if decorative is None:
        return f"<xdr:cNvPr{attrs}/>"
    val = attr("val", decorative) if decorative_attr else ""
    ext = (
        f'<a:extLst><a:ext uri="{DECORATIVE_EXT_URI}">'
        f'<adec:decorative xmlns:adec="{ADEC}"{val}/></a:ext></a:extLst>'
    )
    return f"<xdr:cNvPr{attrs}>{ext}</xdr:cNvPr>"


def pic(props: str, embed: str = "rIdImg") -> str:
    return (
        f"<xdr:pic><xdr:nvPicPr>{props}<xdr:cNvPicPr/></xdr:nvPicPr>"
        f'<xdr:blipFill><a:blip r:embed="{embed}"/><a:stretch><a:fillRect/></a:stretch>'
        '</xdr:blipFill><xdr:spPr><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr>'
        "</xdr:pic>"
    )


def shape(props: str, text: str | None = None, *, text_box: bool = False) -> str:
    body = ""
    if text is not None:
        body = f"<xdr:txBody><a:bodyPr/><a:p><a:r><a:t>{text}</a:t></a:r></a:p></xdr:txBody>"
    box = ' txBox="1"' if text_box else ""
    return (
        f"<xdr:sp><xdr:nvSpPr>{props}<xdr:cNvSpPr{box}/></xdr:nvSpPr>"
        '<xdr:spPr><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr>'
        f"{body}</xdr:sp>"
    )


def connector(props: str) -> str:
    return (
        f"<xdr:cxnSp><xdr:nvCxnSpPr>{props}<xdr:cNvCxnSpPr/></xdr:nvCxnSpPr>"
        '<xdr:spPr><a:prstGeom prst="line"><a:avLst/></a:prstGeom></xdr:spPr></xdr:cxnSp>'
    )


def chart(props: str, rel_id: str = "rIdChart") -> str:
    return (
        f"<xdr:graphicFrame><xdr:nvGraphicFramePr>{props}<xdr:cNvGraphicFramePr/>"
        "</xdr:nvGraphicFramePr><xdr:xfrm/>"
        f'<a:graphic><a:graphicData uri="{C}"><c:chart xmlns:c="{C}" r:id="{rel_id}"/>'
        "</a:graphicData></a:graphic></xdr:graphicFrame>"
    )


def group(props: str, *children: str) -> str:
    return (
        f"<xdr:grpSp><xdr:nvGrpSpPr>{props}<xdr:cNvGrpSpPr/></xdr:nvGrpSpPr>"
        f"<xdr:grpSpPr/>{''.join(children)}</xdr:grpSp>"
    )


def alternate(choice: str, fallback: str, requires: str = "a14") -> str:
    return (
        f'<mc:AlternateContent xmlns:mc="{MC}"><mc:Choice Requires="{requires}">{choice}'
        f"</mc:Choice><mc:Fallback>{fallback}</mc:Fallback></mc:AlternateContent>"
    )


def anchor(obj: str, kind: str = "twoCellAnchor") -> str:
    if kind == "absoluteAnchor":
        layout = '<xdr:pos x="0" y="0"/><xdr:ext cx="100" cy="100"/>'
    elif kind == "oneCellAnchor":
        layout = (
            "<xdr:from><xdr:col>0</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>0</xdr:row>"
            '<xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:ext cx="100" cy="100"/>'
        )
    else:
        layout = (
            "<xdr:from><xdr:col>0</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>0</xdr:row>"
            "<xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:to><xdr:col>2</xdr:col>"
            "<xdr:colOff>0</xdr:colOff><xdr:row>4</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
        )
    return f"<xdr:{kind}>{layout}{obj}<xdr:clientData/></xdr:{kind}>"


def drawing_xml(*anchors: str) -> str:
    return (
        f'{DECL}<xdr:wsDr xmlns:xdr="{XDR}" xmlns:a="{A}" xmlns:r="{REL}">'
        f"{''.join(anchors)}</xdr:wsDr>"
    )


def chart_part_xml(title: str = "Quarterly totals") -> str:
    """Minimal bar chart whose VISIBLE title must not satisfy the alt-text fields."""
    return (
        f'{DECL}<c:chartSpace xmlns:c="{C}" xmlns:a="{A}" xmlns:r="{REL}"><c:chart>'
        f"<c:title><c:tx><c:rich><a:bodyPr/><a:p><a:r><a:t>{title}</a:t></a:r></a:p>"
        "</c:rich></c:tx></c:title><c:plotArea><c:layout/><c:barChart>"
        '<c:barDir val="col"/><c:grouping val="clustered"/><c:ser><c:idx val="0"/>'
        '<c:order val="0"/><c:val><c:numRef><c:f>Data!$A$1:$A$2</c:f></c:numRef></c:val>'
        '</c:ser><c:axId val="1"/><c:axId val="2"/></c:barChart><c:catAx><c:axId val="1"/>'
        '<c:scaling><c:orientation val="minMax"/></c:scaling><c:axPos val="b"/>'
        '<c:crossAx val="2"/></c:catAx><c:valAx><c:axId val="2"/><c:scaling>'
        '<c:orientation val="minMax"/></c:scaling><c:axPos val="l"/><c:crossAx val="1"/>'
        "</c:valAx></c:plotArea></c:chart></c:chartSpace>"
    )


@dataclass
class Drawing:
    """A drawing part with an image and chart available as rIdImg / rIdChart."""

    part: str
    xml: str
    chart_part: str | None = None
    image_part: str | None = None


@dataclass
class Sheet:
    name: str
    part: str
    sheet_id: str
    cells: str = ""
    tail: str = ""
    state: str | None = None
    drawing: Drawing | None = None
    vml_part: str | None = None
    vml_xml: str | None = None
    kind: str = "worksheet"


@dataclass
class Workbook:
    sheets: list[Sheet]
    workbook_head: str = ""
    extra_parts: dict[str, bytes] = field(default_factory=dict)
    extra_defaults: dict[str, str] = field(default_factory=dict)
    workbook_content_type: str = CT_WORKBOOK
    compression: int = zipfile.ZIP_DEFLATED

    def parts(self) -> dict[str, bytes]:
        overrides = {"xl/workbook.xml": self.workbook_content_type}
        parts: dict[str, bytes] = {}
        workbook_rels = []
        sheet_entries = []
        for index, sheet in enumerate(self.sheets, start=1):
            rel_type = "chartsheet" if sheet.kind == "chartsheet" else "worksheet"
            target = sheet.part.removeprefix("xl/")
            workbook_rels.append(
                f'<Relationship Id="rIdSheet{index}" Type="{REL}/{rel_type}" Target="{target}"/>'
            )
            sheet_entries.append(
                f'<sheet{attr("name", sheet.name)} sheetId="{sheet.sheet_id}"'
                f'{attr("state", sheet.state)} r:id="rIdSheet{index}"/>'
            )
            overrides[sheet.part] = CT_CHARTSHEET if sheet.kind == "chartsheet" else CT_WORKSHEET
            parts[sheet.part] = self._sheet_xml(sheet).encode()
            sheet_rels = []
            folder_depth = sheet.part.count("/")
            up = "../" * (folder_depth - 1)
            if sheet.drawing is not None:
                drawing = sheet.drawing
                sheet_rels.append(
                    f'<Relationship Id="rIdDrawing" Type="{REL}/drawing" '
                    f'Target="{up}{drawing.part.removeprefix("xl/")}"/>'
                )
                parts[drawing.part] = drawing.xml.encode()
                overrides[drawing.part] = CT_DRAWING
                chart_part = drawing.chart_part or "xl/charts/chart1.xml"
                image_part = drawing.image_part or "xl/media/image1.png"
                parts.setdefault(chart_part, chart_part_xml().encode())
                parts.setdefault(image_part, PNG_1X1)
                overrides[chart_part] = CT_CHART
                drawing_up = "../" * (drawing.part.count("/") - 1)
                parts[rels_name(drawing.part)] = rels_xml(
                    f'<Relationship Id="rIdImg" Type="{REL}/image" '
                    f'Target="{drawing_up}{image_part.removeprefix("xl/")}"/>'
                    f'<Relationship Id="rIdChart" Type="{REL}/chart" '
                    f'Target="{drawing_up}{chart_part.removeprefix("xl/")}"/>'
                )
            if sheet.vml_part is not None and sheet.vml_xml is not None:
                sheet_rels.append(
                    f'<Relationship Id="rIdVml" Type="{REL}/vmlDrawing" '
                    f'Target="{up}{sheet.vml_part.removeprefix("xl/")}"/>'
                )
                parts[sheet.vml_part] = sheet.vml_xml.encode()
            if sheet_rels:
                parts[rels_name(sheet.part)] = rels_xml("".join(sheet_rels))
        parts["xl/workbook.xml"] = (
            f'{DECL}<workbook xmlns="{MAIN}" xmlns:r="{REL}">{self.workbook_head}'
            f"<sheets>{''.join(sheet_entries)}</sheets></workbook>"
        ).encode()
        parts["xl/_rels/workbook.xml.rels"] = rels_xml("".join(workbook_rels))
        parts["_rels/.rels"] = rels_xml(
            f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="xl/workbook.xml"/>'
        )
        parts.update(self.extra_parts)
        defaults = {
            "rels": "application/vnd.openxmlformats-package.relationships+xml",
            "xml": "application/xml",
            "png": "image/png",
            "vml": CT_VML,
            "bin": "application/octet-stream",
            **self.extra_defaults,
        }
        content_types = (
            f'{DECL}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            + "".join(
                f'<Default Extension="{ext}" ContentType="{ct}"/>' for ext, ct in defaults.items()
            )
            + "".join(
                f'<Override PartName="/{name}" ContentType="{ct}"/>'
                for name, ct in overrides.items()
            )
            + "</Types>"
        )
        return {"[Content_Types].xml": content_types.encode(), **parts}

    def _sheet_xml(self, sheet: Sheet) -> str:
        drawing = '<drawing r:id="rIdDrawing"/>' if sheet.drawing is not None else ""
        legacy = '<legacyDrawing r:id="rIdVml"/>' if sheet.vml_part is not None else ""
        if sheet.kind == "chartsheet":
            return (
                f'{DECL}<chartsheet xmlns="{MAIN}" xmlns:r="{REL}"><sheetViews><sheetView '
                f'workbookViewId="0"/></sheetViews>{sheet.tail}{drawing}</chartsheet>'
            )
        return (
            f'{DECL}<worksheet xmlns="{MAIN}" xmlns:r="{REL}"><sheetData>{sheet.cells}'
            f"</sheetData>{sheet.tail}{drawing}{legacy}</worksheet>"
        )

    def build(self) -> bytes:
        return zip_parts(self.parts(), compression=self.compression)


def rels_name(part: str) -> str:
    folder, _, name = part.rpartition("/")
    return f"{folder}/_rels/{name}.rels"


def rels_xml(body: str) -> bytes:
    return f'{DECL}<Relationships xmlns="{PKG_REL}">{body}</Relationships>'.encode()


def zip_parts(
    parts: dict[str, bytes] | list[tuple[str, bytes]],
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    """Deterministic ZIP (fixed timestamps, given order). A list allows duplicate names."""
    import io
    import warnings

    items = list(parts.items()) if isinstance(parts, dict) else parts
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, content in items:
                info = zipfile.ZipInfo(name, FIXED_TIME)
                info.compress_type = compression
                info.external_attr = 0o600 << 16
                archive.writestr(info, content)
    return buffer.getvalue()


def set_encrypted_flag(data: bytes, entry: str) -> bytes:
    """Set general-purpose bit 0 (encryption) on an entry's local and central headers."""
    out = bytearray(data)
    encoded = entry.encode()
    for signature, flag_offset, name_len_offset, name_offset in (
        (b"PK\x03\x04", 6, 26, 30),
        (b"PK\x01\x02", 8, 28, 46),
    ):
        start = 0
        while (pos := out.find(signature, start)) != -1:
            (name_len,) = struct.unpack_from("<H", out, pos + name_len_offset)
            if bytes(out[pos + name_offset : pos + name_offset + name_len]) == encoded:
                (flags,) = struct.unpack_from("<H", out, pos + flag_offset)
                struct.pack_into("<H", out, pos + flag_offset, flags | 0x1)
            start = pos + 4
    return bytes(out)


def cell_rows(count: int, per_row: int = 100) -> str:
    """`count` numeric cells with minimal markup, `per_row` cells per row."""
    rows = []
    remaining = count
    row = 1
    while remaining > 0:
        n = min(per_row, remaining)
        rows.append(f'<row r="{row}">' + "<c><v>1</v></c>" * n + "</row>")
        remaining -= n
        row += 1
    return "".join(rows)


def simple_workbook(
    *names: str, cells: str = '<row r="1"><c r="A1"><v>1</v></c></row>'
) -> Workbook:
    return Workbook(
        [
            Sheet(name, f"xl/worksheets/sheet{i}.xml", str(i), cells=cells)
            for i, name in enumerate(names, start=1)
        ]
    )
