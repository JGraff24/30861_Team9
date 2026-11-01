"""Synthetic Phase 4 workbooks for Dylan's engine tests (test helper, not production).

Every cell, style, link, merge, table and reference is chosen explicitly by the caller,
so expected findings and corrections are authored from the seeds, never from audit().
"""

import io
import zipfile
from dataclasses import dataclass, field
from xml.sax.saxutils import escape, quoteattr

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
STRICT_MAIN = "http://purl.oclc.org/ooxml/spreadsheetml/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
STRICT_REL = "http://purl.oclc.org/ooxml/officeDocument/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
X14AC = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
CT = {
    "workbook": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    "worksheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
    "styles": "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml",
    "sst": "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml",
    "theme": "application/vnd.openxmlformats-officedocument.theme+xml",
    "table": "application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml",
    "drawing": "application/vnd.openxmlformats-officedocument.drawing+xml",
    "chart": "application/vnd.openxmlformats-officedocument.drawingml.chart+xml",
}
THEME_COLORS = {
    "dk1": "000000",
    "lt1": "FFFFFF",
    "dk2": "44546A",
    "lt2": "E7E6E6",
    "accent1": "336699",
    "accent2": "ED7D31",
    "accent3": "A5A5A5",
    "accent4": "FFC000",
    "accent5": "5B9BD5",
    "accent6": "70AD47",
    "hlink": "0563C1",
    "folHlink": "954F72",
}


@dataclass
class Cell:
    """A cell value. kind: s (shared string), inline, n, b, str (formula string), empty."""

    value: object = None
    kind: str = "s"
    formula: str | None = None
    style: int | None = None
    rich_runs: bool = False


def text(value: str, style: int | None = None) -> Cell:
    return Cell(value, "s", style=style)


def number(value: float, style: int | None = None) -> Cell:
    return Cell(value, "n", style=style)


def formula(expression: str, cached: object = None, kind: str = "n") -> Cell:
    return Cell(cached, kind, formula=expression)


@dataclass
class Link:
    ref: str
    url: str | None = None
    location: str | None = None
    display: str | None = None


@dataclass
class TableDef:
    ref: str
    columns: list[str]
    table_id: str | None = "1"
    name: str = "Table1"
    header_rows: str | None = None
    style: str | None = None
    totals: str | None = None


@dataclass
class Sheet:
    name: str
    cells: dict[str, Cell | str | float] = field(default_factory=dict)
    merges: list[str] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    conditional: str = ""
    validations: str = ""
    tables: list[TableDef] = field(default_factory=list)
    drawing: str | None = None
    state: str | None = None
    extension: str = ""


@dataclass
class Font:
    color: str | None = None  # raw <color .../> attributes, e.g. 'rgb="FFFF0000"'
    size: float = 11
    bold: bool = False


@dataclass
class Workbook:
    sheets: list[Sheet]
    fonts: list[Font] = field(default_factory=lambda: [Font()])
    fills: list[str | None] = field(default_factory=lambda: [None, "gray125"])
    formats: list[tuple[int, int]] = field(default_factory=lambda: [(0, 0)])
    dxfs: list[str] = field(default_factory=list)
    defined_names: list[tuple[str, str]] = field(default_factory=list)
    charts: list[str] = field(default_factory=list)  # series formulas per chart part
    strict: bool = False
    extra_parts: dict[str, bytes] = field(default_factory=dict)
    utf8_bom: bool = False

    @property
    def main(self) -> str:
        return STRICT_MAIN if self.strict else MAIN

    @property
    def rel(self) -> str:
        return STRICT_REL if self.strict else REL

    def build(self) -> bytes:
        parts = self.parts()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in parts.items():
                info = zipfile.ZipInfo(name, (2026, 10, 30, 12, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
        return buffer.getvalue()

    def parts(self) -> dict[str, bytes]:
        strings: list[str] = []
        overrides = {"xl/workbook.xml": CT["workbook"], "xl/styles.xml": CT["styles"]}
        overrides["xl/theme/theme1.xml"] = CT["theme"]
        parts: dict[str, bytes] = {}
        workbook_rels = [
            f'<Relationship Id="rIdStyles" Type="{self.rel}/styles" Target="styles.xml"/>',
            f'<Relationship Id="rIdTheme" Type="{self.rel}/theme" Target="theme/theme1.xml"/>',
        ]
        entries = []
        table_number = 0
        for index, sheet in enumerate(self.sheets, start=1):
            part = f"xl/worksheets/sheet{index}.xml"
            workbook_rels.append(
                f'<Relationship Id="rIdSheet{index}" Type="{self.rel}/worksheet" '
                f'Target="worksheets/sheet{index}.xml"/>'
            )
            state = f' state="{sheet.state}"' if sheet.state else ""
            entries.append(
                f'<sheet name={quoteattr(sheet.name)} sheetId="{index}"{state} '
                f'r:id="rIdSheet{index}"/>'
            )
            overrides[part] = CT["worksheet"]
            sheet_rels: list[str] = []
            table_refs: list[str] = []
            for link_index, link in enumerate(sheet.links, start=1):
                if link.url is not None:
                    sheet_rels.append(
                        f'<Relationship Id="rIdLink{link_index}" Type="{self.rel}/hyperlink" '
                        f'Target={quoteattr(link.url)} TargetMode="External"/>'
                    )
            for table in sheet.tables:
                table_number += 1
                table_part = f"xl/tables/table{table_number}.xml"
                sheet_rels.append(
                    f'<Relationship Id="rIdTable{table_number}" Type="{self.rel}/table" '
                    f'Target="../tables/table{table_number}.xml"/>'
                )
                table_refs.append(f'<tablePart r:id="rIdTable{table_number}"/>')
                overrides[table_part] = CT["table"]
                parts[table_part] = self._table_xml(table).encode()
            if sheet.drawing is not None:
                sheet_rels.append(
                    f'<Relationship Id="rIdDrawing" Type="{self.rel}/drawing" '
                    f'Target="../drawings/drawing{index}.xml"/>'
                )
                overrides[f"xl/drawings/drawing{index}.xml"] = CT["drawing"]
                parts[f"xl/drawings/drawing{index}.xml"] = sheet.drawing.encode()
            if sheet_rels:
                parts[f"xl/worksheets/_rels/sheet{index}.xml.rels"] = (
                    f'{DECL}<Relationships xmlns="{PKG}">{"".join(sheet_rels)}</Relationships>'
                ).encode()
            parts[part] = self._sheet_xml(sheet, strings, table_refs).encode()
        for number, series in enumerate(self.charts, start=1):
            chart = f"xl/charts/chart{number}.xml"
            overrides[chart] = CT["chart"]
            parts[chart] = (
                f'{DECL}<c:chartSpace xmlns:c="{C}" xmlns:a="{A}"><c:chart><c:plotArea>'
                f'<c:barChart><c:ser><c:idx val="0"/><c:cat><c:strRef><c:f>{escape(series)}'
                "</c:f></c:strRef></c:cat></c:ser></c:barChart></c:plotArea></c:chart>"
                "</c:chartSpace>"
            ).encode()
        if strings:
            overrides["xl/sharedStrings.xml"] = CT["sst"]
            workbook_rels.append(
                f'<Relationship Id="rIdStrings" Type="{self.rel}/sharedStrings" '
                'Target="sharedStrings.xml"/>'
            )
            items = "".join(
                f'<si><t xml:space="preserve">{escape(item)}</t></si>' for item in strings
            )
            parts["xl/sharedStrings.xml"] = (
                f'{DECL}<sst xmlns="{self.main}" count="{len(strings)}" '
                f'uniqueCount="{len(strings)}">{items}</sst>'
            ).encode()
        names = ""
        if self.defined_names:
            names = (
                "<definedNames>"
                + "".join(
                    f"<definedName name={quoteattr(name)}>{escape(value)}</definedName>"
                    for name, value in self.defined_names
                )
                + "</definedNames>"
            )
        parts["xl/workbook.xml"] = (
            f'{DECL}<workbook xmlns="{self.main}" xmlns:r="{self.rel}" xmlns:mc="{MC}" '
            f'xmlns:x14ac="{X14AC}" mc:Ignorable="x14ac"><workbookPr/>'
            f"<sheets>{''.join(entries)}</sheets>{names}</workbook>"
        ).encode()
        parts["xl/_rels/workbook.xml.rels"] = (
            f'{DECL}<Relationships xmlns="{PKG}">{"".join(workbook_rels)}</Relationships>'
        ).encode()
        parts["xl/styles.xml"] = self._styles_xml().encode()
        scheme = "".join(
            f'<a:{name}><a:srgbClr val="{value}"/></a:{name}>'
            for name, value in THEME_COLORS.items()
        )
        parts["xl/theme/theme1.xml"] = (
            f'{DECL}<a:theme xmlns:a="{A}" name="Office"><a:themeElements>'
            f'<a:clrScheme name="Office">{scheme}</a:clrScheme></a:themeElements></a:theme>'
        ).encode()
        parts["_rels/.rels"] = (
            f'{DECL}<Relationships xmlns="{PKG}"><Relationship Id="rId1" '
            f'Type="{self.rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>'
        ).encode()
        parts.update(self.extra_parts)
        content_types = (
            f'{DECL}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
            'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            + "".join(
                f'<Override PartName="/{name}" ContentType="{value}"/>'
                for name, value in overrides.items()
            )
            + "</Types>"
        )
        result = {"[Content_Types].xml": content_types.encode(), **parts}
        if self.utf8_bom:
            result = {
                name: b"\xef\xbb\xbf" + data if name.endswith(".xml") else data
                for name, data in result.items()
            }
        return result

    def _cell_xml(self, ref: str, cell: Cell, strings: list[str]) -> str:
        style = f' s="{cell.style}"' if cell.style is not None else ""
        if cell.formula is not None:
            cached = "" if cell.value is None else f"<v>{escape(str(cell.value))}</v>"
            kind = ' t="str"' if cell.kind == "str" else ""
            return f'<c r="{ref}"{style}{kind}><f>{escape(cell.formula)}</f>{cached}</c>'
        if cell.kind == "s":
            if cell.rich_runs:
                return (
                    f'<c r="{ref}"{style} t="inlineStr"><is><r><rPr><b/></rPr>'
                    f"<t>{escape(str(cell.value))}</t></r></is></c>"
                )
            strings.append(str(cell.value))
            return f'<c r="{ref}"{style} t="s"><v>{len(strings) - 1}</v></c>'
        if cell.kind == "inline":
            return (
                f'<c r="{ref}"{style} t="inlineStr"><is><t>{escape(str(cell.value))}</t></is></c>'
            )
        if cell.kind == "empty":
            return f'<c r="{ref}"{style}/>'
        if cell.kind == "b":
            return f'<c r="{ref}"{style} t="b"><v>{int(bool(cell.value))}</v></c>'
        return f'<c r="{ref}"{style}><v>{cell.value}</v></c>'

    def _sheet_xml(self, sheet: Sheet, strings: list[str], table_refs: list[str]) -> str:
        from app.workbooks.refs import parse_cell

        rows: dict[int, list[tuple[int, str]]] = {}
        for ref, raw in sheet.cells.items():
            cell = (
                raw
                if isinstance(raw, Cell)
                else (text(raw) if isinstance(raw, str) else number(raw))
            )
            row, col = parse_cell(ref)
            rows.setdefault(row, []).append((col, self._cell_xml(ref, cell, strings)))
        data = "".join(
            f'<row r="{row}">{"".join(xml for _, xml in sorted(cells))}</row>'
            for row, cells in sorted(rows.items())
        )
        merges = (
            f'<mergeCells count="{len(sheet.merges)}">'
            + "".join(f'<mergeCell ref="{ref}"/>' for ref in sheet.merges)
            + "</mergeCells>"
            if sheet.merges
            else ""
        )
        links = ""
        if sheet.links:
            items = []
            for index, link in enumerate(sheet.links, start=1):
                rid = f' r:id="rIdLink{index}"' if link.url is not None else ""
                location = f" location={quoteattr(link.location)}" if link.location else ""
                display = f" display={quoteattr(link.display)}" if link.display is not None else ""
                items.append(f'<hyperlink ref="{link.ref}"{rid}{location}{display}/>')
            links = f"<hyperlinks>{''.join(items)}</hyperlinks>"
        tables = (
            f'<tableParts count="{len(table_refs)}">{"".join(table_refs)}</tableParts>'
            if table_refs
            else ""
        )
        drawing = '<drawing r:id="rIdDrawing"/>' if sheet.drawing is not None else ""
        return (
            f'{DECL}<worksheet xmlns="{self.main}" xmlns:r="{self.rel}" xmlns:mc="{MC}" '
            f'xmlns:x14ac="{X14AC}" mc:Ignorable="x14ac"><sheetViews><sheetView '
            f'workbookViewId="0"/></sheetViews><sheetFormatPr defaultRowHeight="15" '
            f'x14ac:dyDescent="0.25"/><sheetData>{data}</sheetData>{merges}'
            f"{sheet.conditional}{sheet.validations}{links}"
            '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" '
            f'footer="0.3"/>{drawing}{tables}{sheet.extension}</worksheet>'
        )

    def _table_xml(self, table: TableDef) -> str:
        identifier = f' id="{table.table_id}"' if table.table_id is not None else ""
        header = f' headerRowCount="{table.header_rows}"' if table.header_rows is not None else ""
        totals = f' totalsRowCount="{table.totals}"' if table.totals is not None else ""
        style = f'<tableStyleInfo name="{table.style}" showRowStripes="1"/>' if table.style else ""
        columns = "".join(
            f'<tableColumn id="{index}" name={quoteattr(name)}/>'
            for index, name in enumerate(table.columns, start=1)
        )
        return (
            f'{DECL}<table xmlns="{self.main}"{identifier} name="{table.name}" '
            f'displayName="{table.name}" ref="{table.ref}"{header}{totals}>'
            f'<tableColumns count="{len(table.columns)}">{columns}</tableColumns>{style}</table>'
        )

    def _styles_xml(self) -> str:
        fonts = "".join(
            "<font>"
            + ("<b/>" if font.bold else "")
            + f'<sz val="{font.size}"/>'
            + (f"<color {font.color}/>" if font.color else "")
            + '<name val="Calibri"/></font>'
            for font in self.fonts
        )
        fills = ""
        for fill in self.fills:
            if fill is None:
                fills += '<fill><patternFill patternType="none"/></fill>'
            elif fill == "gray125":
                fills += '<fill><patternFill patternType="gray125"/></fill>'
            else:
                fills += (
                    f'<fill><patternFill patternType="solid"><fgColor {fill}/></patternFill></fill>'
                )
        formats = "".join(
            f'<xf numFmtId="0" fontId="{font}" fillId="{fill}" borderId="0" xfId="0"/>'
            for font, fill in self.formats
        )
        dxfs = (
            f'<dxfs count="{len(self.dxfs)}">' + "".join(self.dxfs) + "</dxfs>" if self.dxfs else ""
        )
        return (
            f'{DECL}<styleSheet xmlns="{self.main}">'
            f'<fonts count="{len(self.fonts)}">{fonts}</fonts>'
            f'<fills count="{len(self.fills)}">{fills}</fills>'
            '<borders count="1"><border/></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>'
            f'</cellStyleXfs><cellXfs count="{len(self.formats)}">{formats}</cellXfs>{dxfs}'
            "</styleSheet>"
        )


def picture_drawing(
    object_id: str = "2", title: str | None = None, descr: str | None = None
) -> str:
    attrs = f'id="{object_id}" name="Picture {object_id}"'
    if title is not None:
        attrs += f" title={quoteattr(title)}"
    if descr is not None:
        attrs += f" descr={quoteattr(descr)}"
    return (
        f'{DECL}<xdr:wsDr xmlns:xdr="{XDR}" xmlns:a="{A}" xmlns:r="{REL}"><xdr:twoCellAnchor>'
        "<xdr:from><xdr:col>3</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>1</xdr:row>"
        "<xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:to><xdr:col>5</xdr:col><xdr:colOff>0"
        "</xdr:colOff><xdr:row>5</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
        f"<xdr:sp><xdr:nvSpPr><xdr:cNvPr {attrs}/><xdr:cNvSpPr/></xdr:nvSpPr>"
        '<xdr:spPr><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr></xdr:sp>'
        "<xdr:clientData/></xdr:twoCellAnchor></xdr:wsDr>"
    )
