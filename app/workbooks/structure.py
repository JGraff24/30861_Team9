"""Workbook structure: main part, sheets, protection, macros and nonempty cells.

Validation and audit share this scan so both see the same name-to-part map.
"""

import re
from dataclasses import dataclass, field
from typing import Literal
from xml.etree.ElementTree import Element

from app.workbooks import errors
from app.workbooks.package import (
    CONTENT_TYPES_PART,
    XML_EXTENSIONS,
    OoxmlPackage,
    Relationship,
    rel_type_is,
)
from app.workbooks.xmlsafe import local_name, namespace

MAIN_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "http://purl.oclc.org/ooxml/spreadsheetml/main",
    }
)
R_NAMESPACES = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
)
WORKBOOK_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
MACRO_CONTENT_MARKERS = ("macroenabled", "vbaproject", "macrosheet")
MACRO_REL_SUFFIXES = frozenset(
    {"vbaproject", "vbaprojectsignature", "xlmacrosheet", "xlintlmacrosheet"}
)
SheetKind = Literal["worksheet", "chartsheet", "dialogsheet"]
SHEET_CONTENT_TYPES: dict[SheetKind, str] = {
    "worksheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
    "chartsheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.chartsheet+xml",
    "dialogsheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.dialogsheet+xml",
}
TRUE_VALUES = frozenset({"1", "true"})
#: Children of a sheet root that reference drawing-like content, recorded for the audit.
SHEET_REFERENCE_ELEMENTS = frozenset(
    {"drawing", "legacyDrawing", "legacyDrawingHF", "picture", "oleObjects", "controls"}
)


@dataclass(frozen=True)
class SheetReference:
    """A direct child of the sheet root, with its one-based same-name sibling index."""

    element: str
    index: int
    rel_id: str | None


#: Rich-value cells recorded per sheet; any beyond this are counted, not listed.
MAX_RICH_VALUE_CELLS_PER_SHEET = 1_000
#: An A1-style cell reference (Excel's grid is at most XFD1048576).
CELL_REF = re.compile(r"[A-Za-z]{1,3}[0-9]{1,7}", re.ASCII)


@dataclass(frozen=True)
class RichValueCell:
    """A cell carrying value metadata (vm), e.g. a Place-in-Cell picture or IMAGE() result."""

    ref: str | None  # the cell's r attribute when present and well-formed
    path: tuple[tuple[str, int], ...]  # one-based element path to the <c> element


@dataclass
class SheetInfo:
    name: str
    sheet_id: str
    state: str
    kind: SheetKind
    part: str
    nonempty_cells: int = 0
    references: list[SheetReference] = field(default_factory=list)
    root_name: str = ""
    rich_value_cells: list[RichValueCell] = field(default_factory=list)
    rich_value_cells_omitted: int = 0


@dataclass
class WorkbookStructure:
    workbook_part: str
    sheets: list[SheetInfo]

    @property
    def nonempty_cells(self) -> int:
        return sum(sheet.nonempty_cells for sheet in self.sheets)

    @property
    def worksheet_parts(self) -> dict[str, str]:
        return {sheet.name: sheet.part for sheet in self.sheets}


def r_attr(elem: Element, name: str) -> str | None:
    for ns in R_NAMESPACES:
        value = elem.get(f"{{{ns}}}{name}")
        if value is not None:
            return value
    return None


def _check_macros(package: OoxmlPackage) -> None:
    for name in package.entry_names:
        lowered = name.casefold()
        if lowered.rpartition("/")[2] == "vbaproject.bin" or "/macrosheets/" in f"/{lowered}":
            errors.macro("vba_or_macrosheet_part")
    for content_type in package.all_content_types:
        if any(marker in content_type.casefold() for marker in MACRO_CONTENT_MARKERS):
            errors.macro("macro_content_type")


def _check_relationship_parts(package: OoxmlPackage) -> None:
    """Parse every .rels part: rejects macro links and traversal anywhere in the package."""
    for name in package.entry_names:
        if not name.casefold().endswith(".rels"):
            continue
        for rel in package.parse_rels(name).values():
            if rel.rel_type.rpartition("/")[2].casefold() in MACRO_REL_SUFFIXES:
                errors.macro("macro_relationship")


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip() in TRUE_VALUES


def _has_secret_attribute(elem: Element) -> bool:
    return any("password" in key.casefold() or "hashvalue" in key.casefold() for key in elem.attrib)


def _check_workbook_protection(root: Element) -> None:
    for child in root:
        name = local_name(child.tag)
        if name == "workbookProtection":
            locks = ("lockStructure", "lockWindows", "lockRevision")
            if any(_truthy(child.get(lock)) for lock in locks) or _has_secret_attribute(child):
                errors.protected("workbook")
        elif name == "fileSharing" and _has_secret_attribute(child):
            errors.protected("workbook")


def _sheet_kind(rel: Relationship) -> SheetKind:
    for kind in ("worksheet", "chartsheet", "dialogsheet"):
        if rel_type_is(rel.rel_type, kind):
            return kind
    if rel.rel_type.rpartition("/")[2].casefold() in MACRO_REL_SUFFIXES:
        errors.macro("macro_sheet")
    errors.invalid("unknown_sheet_relationship")


def read_workbook(package: OoxmlPackage, max_worksheets: int | None) -> WorkbookStructure:
    """Locate the workbook part and its sheets. Enforces the worksheet limit if given."""
    _check_macros(package)
    _check_relationship_parts(package)
    office = [
        rel
        for rel in package.relationships("").values()
        if rel_type_is(rel.rel_type, "officeDocument")
    ]
    if len(office) != 1 or office[0].external or office[0].resolved is None:
        errors.invalid("missing_workbook_part")
    workbook_part = office[0].resolved
    content_type = package.content_type(workbook_part)
    if content_type != WORKBOOK_CONTENT_TYPE:
        errors.invalid("workbook_content_type")
    root = package.parse_xml(workbook_part)
    if local_name(root.tag) != "workbook" or namespace(root.tag) not in MAIN_NAMESPACES:
        errors.invalid("workbook_root")
    _check_workbook_protection(root)
    sheet_elements = [
        sheet
        for child in root
        if local_name(child.tag) == "sheets"
        for sheet in child
        if local_name(sheet.tag) == "sheet"
    ]
    if not sheet_elements:
        errors.invalid("no_sheets")
    if max_worksheets is not None and len(sheet_elements) > max_worksheets:
        errors.limit_exceeded("max_worksheets", max_worksheets)
    rels = package.relationships(workbook_part)
    sheets: list[SheetInfo] = []
    seen_names: set[str] = set()
    seen_parts: set[str] = set()
    for elem in sheet_elements:
        name = elem.get("name")
        sheet_id = elem.get("sheetId", "")
        rel_id = r_attr(elem, "id")
        if not name or not sheet_id or rel_id is None or rel_id not in rels:
            errors.invalid("bad_sheet_entry")
        rel = rels[rel_id]
        kind = _sheet_kind(rel)
        if rel.external or rel.resolved is None:
            errors.invalid("missing_sheet_part")
        part = rel.resolved
        if name.casefold() in seen_names or part.casefold() in seen_parts:
            errors.invalid("duplicate_sheet")
        if package.content_type(part) != SHEET_CONTENT_TYPES[kind]:
            errors.invalid("sheet_content_type")
        seen_names.add(name.casefold())
        seen_parts.add(part.casefold())
        sheets.append(SheetInfo(name, sheet_id, elem.get("state", "visible"), kind, part))
    return WorkbookStructure(workbook_part, sheets)


class _SheetScanner:
    """Streaming sheet scan: nonempty cells, protection and drawing references."""

    def __init__(self, sheet: SheetInfo, cell_budget: int | None) -> None:
        self.sheet = sheet
        self.cell_budget = cell_budget
        self.cells_in_open_c = 0
        self.sibling_counts: dict[str, int] = {}
        self.row_counts: dict[str, int] = {}
        self.cell_counts: dict[str, int] = {}

    def on_start(self, elem: Element, stack: list[Element]) -> None:
        name = local_name(elem.tag)
        if not stack:
            self.sheet.root_name = name
            if name != self.sheet.kind or namespace(elem.tag) not in MAIN_NAMESPACES:
                errors.invalid("sheet_root")
            return
        if name == "c":
            self.cells_in_open_c += 1
        if len(stack) == 2 and local_name(stack[1].tag) == "sheetData":
            self.row_counts[name] = self.row_counts.get(name, 0) + 1
            self.cell_counts = {}
        elif len(stack) == 3 and local_name(stack[1].tag) == "sheetData":
            self.cell_counts[name] = self.cell_counts.get(name, 0) + 1
            if name == "c" and elem.get("vm") is not None:
                self._record_rich_value(elem, stack)
        if len(stack) != 1:
            return
        index = self.sibling_counts.get(name, 0) + 1
        self.sibling_counts[name] = index
        if name == "sheetProtection" and self._protected(elem):
            errors.protected("worksheet")
        if name in SHEET_REFERENCE_ELEMENTS:
            self.sheet.references.append(SheetReference(name, index, r_attr(elem, "id")))

    def _record_rich_value(self, cell: Element, stack: list[Element]) -> None:
        if len(self.sheet.rich_value_cells) >= MAX_RICH_VALUE_CELLS_PER_SHEET:
            self.sheet.rich_value_cells_omitted += 1
            return
        row = local_name(stack[2].tag)
        path = (
            (self.sheet.root_name, 1),
            ("sheetData", self.sibling_counts.get("sheetData", 1)),
            (row, self.row_counts[row]),
            ("c", self.cell_counts["c"]),
        )
        ref = cell.get("r")
        self.sheet.rich_value_cells.append(
            RichValueCell(ref if ref is not None and CELL_REF.fullmatch(ref) else None, path)
        )

    def _protected(self, elem: Element) -> bool:
        if _has_secret_attribute(elem):
            return True
        if self.sheet.kind == "chartsheet":
            return _truthy(elem.get("content")) or _truthy(elem.get("objects"))
        return _truthy(elem.get("sheet"))

    def on_end(self, elem: Element, stack: list[Element]) -> bool:
        if local_name(elem.tag) == "c":
            self.cells_in_open_c -= 1
            if is_nonempty_cell(elem):
                self.sheet.nonempty_cells += 1
                if self.cell_budget is not None and self.sheet.nonempty_cells > self.cell_budget:
                    raise _CellBudgetExceeded
            return False
        return self.cells_in_open_c > 0


class _CellBudgetExceeded(Exception):
    pass


def is_nonempty_cell(cell: Element) -> bool:
    """A formula, a nonempty stored value, or nonempty inline-string text."""
    for child in cell:
        name = local_name(child.tag)
        if name == "f":
            return True
        if name == "v" and child.text:
            return True
        if name == "is" and any(local_name(node.tag) == "t" and node.text for node in child.iter()):
            return True
    return False


def scan_sheets(package: OoxmlPackage, structure: WorkbookStructure, max_cells: int | None) -> None:
    """Stream every sheet (hidden included), stopping as soon as the cell limit is passed."""
    counted = 0
    for sheet in structure.sheets:
        scanner = _SheetScanner(sheet, None if max_cells is None else max_cells - counted)
        try:
            package.stream_xml(sheet.part, scanner.on_start, scanner.on_end)
        except _CellBudgetExceeded:
            assert max_cells is not None
            errors.limit_exceeded("max_nonempty_cells", max_cells)
        counted += sheet.nonempty_cells


def check_remaining_xml(package: OoxmlPackage) -> None:
    """Every XML part must be readable and free of DTDs/entities."""
    for name in package.entry_names:
        if name in package.parsed_parts or name == CONTENT_TYPES_PART:
            continue
        if name.casefold().endswith(XML_EXTENSIONS):
            package.stream_xml(name)
