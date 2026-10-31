"""Immutable source inventory shared by the Phase 4 audit and the correction editor.

The audit and every correction stage rebuild this inventory from the same raw bytes, so
review context, decision preflight and edits all agree on targets and safety profiles.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from xml.etree.ElementTree import Element

from app.contracts.models import ProcessingLimits
from app.workbooks.cells import (
    RichText,
    WorksheetModel,
    checked_range,
    malformed,
    read_shared_strings,
    read_worksheet,
)
from app.workbooks.formulas import contains_hyperlink, scan_formula
from app.workbooks.package import OoxmlPackage, Relationship, rel_type_is
from app.workbooks.refs import Rect
from app.workbooks.structure import SheetInfo, WorkbookStructure, r_attr
from app.workbooks.styles import Stylesheet, read_stylesheet
from app.workbooks.validation import inspect_package
from app.workbooks.xmlsafe import local_name, namespace

XmlPath = tuple[tuple[str, int], ...]
STRICT_MAIN = "http://purl.oclc.org/ooxml/spreadsheetml/main"
#: Package areas whose contents can hold sheet references the editor does not rewrite.
OPAQUE_REFERENCE_PREFIXES = {
    "xl/externallinks/": "an external workbook link",
    "xl/pivottables/": "a pivot table",
    "xl/pivotcache/": "a pivot cache",
    "xl/slicers/": "a slicer",
    "xl/slicercaches/": "a slicer cache",
    "xl/timelines/": "a timeline",
    "xl/timelinecaches/": "a timeline cache",
    "xl/querytables/": "a query table",
    "xl/connections.xml": "a data connection",
    "xl/model/": "a data model",
    "xl/ctrlprops/": "a form control",
    "xl/charts/chartex": "an extended chart",
    "_xmlsignatures/": "a digital signature",
}
SPREADSHEET_EMBEDDINGS = (".xls", ".xlsx", ".xlsm", ".xlsb", ".bin")


@dataclass(frozen=True)
class TableColumn:
    name: str
    has_formula: bool


@dataclass
class TableInfo:
    part: str
    sheet_part: str
    sheet_name: str
    table_id: str | None
    name: str | None
    ref: str
    rect: Rect
    header_count: str | None  # raw headerRowCount (None means the default of 1)
    totals_count: int
    columns: list[TableColumn]
    table_type: str
    has_connection: bool
    has_extension: bool
    style_name: str | None
    has_auto_filter: bool
    root_name: str
    owners: int = 1
    formulas: list[str] = field(default_factory=list)

    @property
    def header_rows(self) -> int | None:
        if self.header_count is None:
            return 1
        try:
            return int(self.header_count)
        except ValueError:
            return None


@dataclass(frozen=True)
class DefinedName:
    name: str
    local_sheet: str | None
    text: str
    path: XmlPath


@dataclass
class SheetModel:
    info: SheetInfo
    position: int  # one-based index of the <sheet> element under <sheets>
    worksheet: WorksheetModel | None
    tables: list[TableInfo] = field(default_factory=list)
    relationships: dict[str, Relationship] = field(default_factory=dict)


@dataclass
class WorkbookInventory:
    package: OoxmlPackage
    structure: WorkbookStructure
    strict: bool
    shared_strings: list[RichText]
    shared_strings_part: str | None
    styles: Stylesheet
    sheets: list[SheetModel]
    defined_names: list[DefinedName]
    chart_formulas: dict[str, list[str]]
    drawing_owners: dict[str, list[str]]
    signed: bool
    rename_blockers: list[str]
    drawing_hyperlinks: bool

    def sheet(self, name: str) -> SheetModel | None:
        return next((model for model in self.sheets if model.info.name == name), None)

    @property
    def all_tables(self) -> list[TableInfo]:
        return [table for model in self.sheets for table in model.tables]


def _child(element: Element, name: str) -> Element | None:
    return next((child for child in element if local_name(child.tag) == name), None)


def _workbook_parts(package: OoxmlPackage, workbook_part: str) -> dict[str, str | None]:
    found: dict[str, str | None] = {"sharedStrings": None, "styles": None, "theme": None}
    for rel in package.relationships(workbook_part).values():
        for kind in found:
            if rel_type_is(rel.rel_type, kind) and not rel.external and found[kind] is None:
                found[kind] = rel.resolved
    return found


def _read_table(package: OoxmlPackage, part: str, sheet: SheetInfo, owners: int) -> TableInfo:
    root = package.parse_xml(part)
    if local_name(root.tag) != "table":
        malformed(f"table part {part} has no table root")
    columns: list[TableColumn] = []
    formulas: list[str] = []
    container = _child(root, "tableColumns")
    if container is not None:
        for column in container:
            if local_name(column.tag) != "tableColumn":
                continue
            column_formulas = [
                node.text or ""
                for node in column
                if local_name(node.tag) in ("calculatedColumnFormula", "totalsRowFormula")
            ]
            formulas.extend(column_formulas)
            columns.append(TableColumn(column.get("name", ""), bool(column_formulas)))
    ref = root.get("ref", "")
    totals = root.get("totalsRowCount")
    try:
        totals_count = int(totals) if totals is not None else 0
    except ValueError:
        totals_count = -1
    style = _child(root, "tableStyleInfo")
    return TableInfo(
        part,
        sheet.part,
        sheet.name,
        root.get("id"),
        root.get("name") or root.get("displayName"),
        ref,
        checked_range(ref, part, "table"),
        root.get("headerRowCount"),
        totals_count,
        columns,
        root.get("tableType", "worksheet"),
        root.get("connectionId") is not None,
        _child(root, "extLst") is not None,
        style.get("name") if style is not None else None,
        _child(root, "autoFilter") is not None,
        local_name(root.tag),
        owners,
        formulas,
    )


def _defined_names(root: Element, sheets: list[SheetInfo]) -> list[DefinedName]:
    result: list[DefinedName] = []
    container_index = 0
    for child in root:
        if local_name(child.tag) != "definedNames":
            continue
        container_index += 1
        position = 0
        for name in child:
            if local_name(name.tag) != "definedName":
                continue
            position += 1
            local = name.get("localSheetId")
            sheet_name = None
            if local is not None:
                try:
                    sheet_name = sheets[int(local)].name
                except (ValueError, IndexError):
                    sheet_name = None
            result.append(
                DefinedName(
                    name.get("name", ""),
                    sheet_name,
                    name.text or "",
                    (
                        (local_name(root.tag), 1),
                        ("definedNames", container_index),
                        ("definedName", position),
                    ),
                )
            )
    return result


def _formula_blocker(text: str, where: str) -> str | None:
    scan = scan_formula(text)
    if scan.unsupported is not None:
        return f"a formula that could not be parsed in {where}"
    if any(token.external for token in scan.sheet_tokens):
        return f"an external workbook reference in {where}"
    if any(len(token.names) > 1 for token in scan.sheet_tokens):
        return f"a 3D sheet reference in {where}"
    constructors = {"INDIRECT", "EVALUATE"} & set(scan.functions)
    if constructors:
        return f"{sorted(constructors)[0]} reference construction in {where}"
    if contains_hyperlink(text):
        return f"a HYPERLINK formula in {where}"
    return None


def build_inventory(data: bytes, limits: ProcessingLimits) -> WorkbookInventory:
    package, structure = inspect_package(data, limits, enforce_content_limits=False)
    root = package.parse_xml(structure.workbook_part)
    strict = namespace(root.tag) == STRICT_MAIN
    parts = _workbook_parts(package, structure.workbook_part)
    shared = read_shared_strings(package, parts["sharedStrings"])
    styles = read_stylesheet(package, parts["styles"], parts["theme"])
    blockers: list[str] = []
    entries = [name.casefold() for name in package.entry_names]
    signed = any(name.startswith("_xmlsignatures/") for name in entries)
    for prefix, label in OPAQUE_REFERENCE_PREFIXES.items():
        if any(name.startswith(prefix) for name in entries):
            blockers.append(label)
    if any(
        name.startswith("xl/embeddings/") and name.endswith(SPREADSHEET_EMBEDDINGS)
        for name in entries
    ):
        blockers.append("an embedded workbook")
    for child in root:
        child_name = local_name(child.tag)
        if child_name == "externalReferences":
            blockers.append("an external workbook link")
        elif child_name == "pivotCaches":
            blockers.append("a pivot cache")
    sheet_positions = {
        id(info): position for position, info in enumerate(structure.sheets, start=1)
    }
    # Drawing and table parts referenced by more than one sheet make ownership ambiguous.
    drawing_owners: dict[str, list[str]] = defaultdict(list)
    table_owners: dict[str, int] = defaultdict(int)
    sheet_rels: dict[str, dict[str, Relationship]] = {}
    for info in structure.sheets:
        rels = package.relationships(info.part)
        sheet_rels[info.part] = rels
        for rel in rels.values():
            if rel.resolved is None or rel.external:
                continue
            if rel_type_is(rel.rel_type, "drawing"):
                drawing_owners[rel.resolved].append(info.name)
            elif rel_type_is(rel.rel_type, "table"):
                table_owners[rel.resolved] += 1
    models: list[SheetModel] = []
    for info in structure.sheets:
        worksheet = None
        if info.kind == "worksheet":
            worksheet = read_worksheet(package, info.part, shared)
        model = SheetModel(info, sheet_positions[id(info)], worksheet, [], sheet_rels[info.part])
        if worksheet is not None:
            for rel_id in worksheet.table_rel_ids:
                table_rel = model.relationships.get(rel_id)
                if table_rel is None or table_rel.external or table_rel.resolved is None:
                    malformed(f"table relationship {rel_id[:40]!r} in {info.part}")
                if not rel_type_is(table_rel.rel_type, "table"):
                    malformed(f"table relationship type in {info.part}")
                model.tables.append(
                    _read_table(package, table_rel.resolved, info, table_owners[table_rel.resolved])
                )
        models.append(model)
    defined = _defined_names(root, structure.sheets)
    chart_formulas: dict[str, list[str]] = {}
    drawing_hyperlinks = False
    for name in package.entry_names:
        lowered = name.casefold()
        if lowered.startswith("xl/charts/chart") and lowered.endswith(".xml"):
            chart_root = package.parse_xml(name)
            chart_formulas[name] = [
                node.text or "" for node in chart_root.iter() if local_name(node.tag) == "f"
            ]
            if any(local_name(node.tag) == "externalData" for node in chart_root.iter()):
                blockers.append("a chart with embedded or external data")
        elif lowered.startswith("xl/drawings/") and lowered.endswith(".xml"):
            drawing_root = package.parse_xml(name)
            if any(
                local_name(node.tag) in ("hlinkClick", "hlinkHover") for node in drawing_root.iter()
            ):
                drawing_hyperlinks = True
        elif lowered.startswith("xl/drawings/") and lowered.endswith(".vml"):
            vml = package.parse_xml(name)
            if any(local_name(node.tag).startswith("Fmla") for node in vml.iter()):
                blockers.append("a legacy form control formula")
    if drawing_hyperlinks:
        blockers.append("drawing hyperlinks whose destinations cannot be rewritten safely")
    for model in models:
        worksheet = model.worksheet
        if worksheet is None:
            continue
        where = f"worksheet {model.info.name}"
        if worksheet.extension_formula_count or worksheet.extension_reference_unsupported:
            blockers.append(f"extension formulas in {where}")
        if worksheet.alternate_content:
            blockers.append(f"alternate content in {where}")
        for cell in worksheet.cells.values():
            if cell.formula is None:
                continue
            if cell.formula_kind in ("shared", "array", "dataTable"):
                blockers.append(f"shared, array or data-table formulas in {where}")
                break
            if cell.has_value_metadata:
                blockers.append(f"dynamic array formulas in {where}")
                break
            blocker = _formula_blocker(cell.formula, where)
            if blocker is not None:
                blockers.append(blocker)
                break
        for text in [
            *worksheet.validation_formulas,
            *worksheet.extension_formulas,
            *[f for c in worksheet.conditionals for f in c.formulas],
        ]:
            blocker = _formula_blocker(text, where)
            if blocker is not None:
                blockers.append(blocker)
                break
        for table in model.tables:
            if any(scan_formula(text).sheet_tokens for text in table.formulas):
                blockers.append(f"table formulas with sheet references in {where}")
    for defined_name in defined:
        blocker = _formula_blocker(defined_name.text, "a defined name")
        if blocker is not None:
            blockers.append(blocker)
            break
    for chart, formulas in chart_formulas.items():
        for text in formulas:
            blocker = _formula_blocker(text, f"chart part {chart}")
            if blocker is not None:
                blockers.append(blocker)
                break
    for model in models:
        if model.worksheet is None:
            continue
        for link in model.worksheet.hyperlinks:
            if link.location is not None:
                blocker = _formula_blocker(link.location, "a hyperlink location")
                if blocker is not None:
                    blockers.append(blocker)
                    break
    return WorkbookInventory(
        package,
        structure,
        strict,
        shared,
        parts["sharedStrings"],
        styles,
        models,
        defined,
        chart_formulas,
        dict(drawing_owners),
        signed,
        sorted(set(blockers)),
        drawing_hyperlinks,
    )


def sheet_reference_rel_id(element: Element) -> str | None:
    return r_attr(element, "id")
