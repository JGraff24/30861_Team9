"""Worksheet cell model for the Phase 4 rules: resolved text, styles and sheet metadata.

One streaming pass per worksheet keeps memory bounded by the largest non-cell subtree.
Cell coordinates follow specification 2.1.0 section 6: an omitted row `r` is the next
row, an omitted cell `r` is the next column, and explicit coordinates must be in bounds,
consistent with their row and unique. Inconsistent coordinates are a controlled failure.
"""

from dataclasses import dataclass, field
from typing import NoReturn
from xml.etree.ElementTree import Element

from app.contracts.errors import ProjectException
from app.workbooks.package import OoxmlPackage
from app.workbooks.refs import MAX_COL, MAX_ROW, CellReferenceError, Rect, parse_cell, parse_range
from app.workbooks.xmlsafe import local_name, namespace

XmlPath = tuple[tuple[str, int], ...]
STRING_TYPES = frozenset({"s", "inlineStr", "str"})
#: Unicode White_Space (specification section 6); U+200B is deliberately absent.
WHITE_SPACE = "".join(
    chr(code)
    for code in (
        *range(0x09, 0x0E),
        0x20,
        0x85,
        0xA0,
        0x1680,
        *range(0x2000, 0x200B),
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
    )
)


def malformed(detail: str) -> NoReturn:
    """A structural reference a rule needs cannot be interpreted: fail, never call it clean."""
    raise ProjectException(
        500,
        "WORKBOOK_STRUCTURE_INVALID",
        f"The workbook could not be interpreted safely: {detail}.",
    )


def ws_trim(text: str) -> str:
    return text.strip(WHITE_SPACE)


@dataclass(frozen=True)
class RichText:
    text: str
    has_runs: bool  # stored as rich-text runs (<r>)
    styled_runs: bool  # at least one run carries its own run properties


def rich_text(element: Element) -> RichText:
    """Concatenate t and r/t text in document order, excluding phonetic runs (rPh)."""
    parts: list[str] = []
    has_runs = styled = False
    for child in element:
        name = local_name(child.tag)
        if name == "t":
            parts.append(child.text or "")
        elif name == "r":
            has_runs = True
            for node in child:
                node_name = local_name(node.tag)
                if node_name == "t":
                    parts.append(node.text or "")
                elif node_name == "rPr":
                    styled = True
    return RichText("".join(parts), has_runs, styled)


def read_shared_strings(package: OoxmlPackage, part: str | None) -> list[RichText]:
    if part is None:
        return []
    root = package.parse_xml(part)
    if local_name(root.tag) != "sst":
        malformed("the shared-string table root is not sst")
    return [rich_text(item) for item in root if local_name(item.tag) == "si"]


@dataclass
class Cell:
    row: int
    col: int
    path: XmlPath  # /worksheet[1]/sheetData[1]/row[i]/c[j]
    cell_type: str  # the t attribute, default "n"
    style: int | None  # the s attribute
    formula: str | None  # formula text (shared dependents inherit the master's text)
    formula_kind: str | None  # f/@t: normal, shared, array, dataTable
    formula_master: bool  # this f element stores the formula text itself
    value: str | None  # raw v text
    text: str | None  # resolved display text for string/number/boolean/error content
    rich: RichText | None  # string content with run structure, when a string type
    has_value_metadata: bool  # vm/cm attributes (rich values, dynamic arrays)
    child_names: tuple[str, ...]

    @property
    def ref(self) -> str:
        from app.workbooks.refs import cell_ref

        return cell_ref(self.row, self.col)

    @property
    def populated(self) -> bool:
        """Section 6 rule population: formula, or nonblank resolved content."""
        if self.formula is not None:
            return True
        if self.cell_type in STRING_TYPES:
            return self.text is not None and bool(ws_trim(self.text))
        return bool(self.value)

    @property
    def plain_string(self) -> bool:
        """Stored text without a formula or rich-text runs."""
        return (
            self.formula is None
            and self.cell_type in ("s", "inlineStr")
            and self.rich is not None
            and not self.rich.has_runs
        )


@dataclass(frozen=True)
class Hyperlink:
    path: XmlPath
    ref: str
    rel_id: str | None
    location: str | None
    display: str | None
    tooltip: str | None


@dataclass(frozen=True)
class MergeDecl:
    path: XmlPath
    ref: str


@dataclass(frozen=True)
class ConditionalDecl:
    path: XmlPath
    sqref: str
    rule_types: tuple[str, ...]
    dxf_ids: tuple[int, ...]
    formulas: tuple[str, ...]
    has_extension: bool


@dataclass(frozen=True)
class ExtensionConditional:
    """x14 conditional formatting stored in the worksheet extension list."""

    sqref: str


@dataclass
class WorksheetModel:
    part: str
    cells: dict[tuple[int, int], Cell] = field(default_factory=dict)
    row_styles: dict[int, int] = field(default_factory=dict)
    column_styles: list[tuple[int, int, int]] = field(default_factory=list)
    merges: list[MergeDecl] = field(default_factory=list)
    hyperlinks: list[Hyperlink] = field(default_factory=list)
    conditionals: list[ConditionalDecl] = field(default_factory=list)
    extension_conditionals: list[ExtensionConditional] = field(default_factory=list)
    validation_formulas: list[str] = field(default_factory=list)
    table_rel_ids: list[str] = field(default_factory=list)
    auto_filter: str | None = None
    #: Formulas or sheet references held inside the worksheet extension list (x14 data
    #: validation, sparklines, slicers). They make reference rewriting unprovable.
    extension_formula_count: int = 0
    extension_names: set[str] = field(default_factory=set)
    root_children: list[str] = field(default_factory=list)
    alternate_content: bool = False

    def rows_with_cells(self) -> list[int]:
        return sorted({row for row, _ in self.cells})


class _WorksheetReader:
    def __init__(self, model: WorksheetModel, shared: list[RichText]) -> None:
        self.model = model
        self.shared = shared
        self.counters: list[dict[str, int]] = []
        self.paths: list[XmlPath] = []
        self.current_row = 0
        self.current_col = 0
        self.shared_masters: dict[str, str] = {}

    def on_start(self, elem: Element, stack: list[Element]) -> None:
        name = local_name(elem.tag)
        depth = len(stack)
        if depth == 0:
            self.counters, self.paths = [{}], [((name, 1),)]
            return
        siblings = self.counters[depth - 1]
        siblings[name] = siblings.get(name, 0) + 1
        del self.counters[depth:], self.paths[depth:]
        self.paths.append((*self.paths[depth - 1], (name, siblings[name])))
        self.counters.append({})
        if depth == 1:
            self.model.root_children.append(name)
        in_sheet_data = depth >= 2 and local_name(stack[1].tag) == "sheetData"
        if in_sheet_data and depth == 2 and name == "row":
            self._start_row(elem)
        elif in_sheet_data and depth == 3 and name == "c":
            self._start_cell(elem)

    def _start_row(self, elem: Element) -> None:
        stored = elem.get("r")
        if stored is None:
            row = self.current_row + 1
        else:
            try:
                row = int(stored)
            except ValueError:
                malformed(f"row reference {stored[:20]!r} in {self.model.part}")
        if not 1 <= row <= MAX_ROW:
            malformed(f"row {row} is outside the grid in {self.model.part}")
        self.current_row = row
        self.current_col = 0
        if elem.get("customFormat", "").strip() in ("1", "true") and elem.get("s") is not None:
            try:
                self.model.row_styles[row] = int(elem.get("s", ""))
            except ValueError:
                malformed(f"row style in {self.model.part}")

    def _start_cell(self, elem: Element) -> None:
        stored = elem.get("r")
        if stored is None:
            row, col = self.current_row, self.current_col + 1
            if col > MAX_COL:
                malformed(f"inferred column beyond the grid in {self.model.part}")
        else:
            try:
                row, col = parse_cell(stored)
            except CellReferenceError:
                malformed(f"cell reference {stored[:20]!r} in {self.model.part}")
            if "$" in stored or row != self.current_row:
                malformed(f"cell reference {stored[:20]!r} disagrees with its row")
        if (row, col) in self.model.cells:
            malformed(f"duplicate cell {stored or col!r} in {self.model.part}")
        self.current_col = col
        self.pending = (row, col)

    def on_end(self, elem: Element, stack: list[Element]) -> bool:
        depth = len(stack)
        in_sheet_data = depth >= 2 and local_name(stack[1].tag) == "sheetData"
        if in_sheet_data:
            if depth == 3 and local_name(elem.tag) == "c":
                self._finish_cell(elem, self.paths[depth])
                return False
            return depth > 3
        if depth == 1:
            self._finish_section(elem, self.paths[depth])
            return False
        return depth > 1

    def _finish_cell(self, elem: Element, path: XmlPath) -> None:
        row, col = self.pending
        cell_type = elem.get("t", "n")
        style_text = elem.get("s")
        try:
            style = int(style_text) if style_text is not None else None
        except ValueError:
            malformed(f"cell style index in {self.model.part}")
        formula = formula_kind = None
        master = False
        value = None
        rich: RichText | None = None
        names: list[str] = []
        for child in elem:
            name = local_name(child.tag)
            names.append(name)
            if name == "f":
                formula_kind = child.get("t", "normal")
                formula_text = child.text or ""
                shared_index = child.get("si")
                if formula_kind == "shared" and shared_index is not None:
                    if formula_text:
                        self.shared_masters[shared_index] = formula_text
                        master = True
                    else:
                        formula_text = self.shared_masters.get(shared_index, "")
                else:
                    master = True
                formula = formula_text
            elif name == "v":
                value = child.text or ""
            elif name == "is":
                rich = rich_text(child)
        text: str | None = None
        if cell_type == "s":
            if value is not None:
                try:
                    rich = self.shared[int(value)]
                except (ValueError, IndexError):
                    malformed(f"shared-string index in {self.model.part}")
                text = rich.text
        elif cell_type == "inlineStr":
            text = rich.text if rich is not None else None
        elif value is not None:
            text = value
            if cell_type == "str":
                rich = RichText(value, False, False)
        self.model.cells[(row, col)] = Cell(
            row,
            col,
            path,
            cell_type,
            style,
            formula,
            formula_kind,
            master,
            value,
            text,
            rich,
            elem.get("vm") is not None or elem.get("cm") is not None,
            tuple(names),
        )

    def _finish_section(self, elem: Element, path: XmlPath) -> None:
        name = local_name(elem.tag)
        model = self.model
        if name == "cols":
            for col in elem:
                if local_name(col.tag) == "col" and col.get("style") is not None:
                    try:
                        model.column_styles.append(
                            (
                                int(col.get("min", "")),
                                int(col.get("max", "")),
                                int(col.get("style", "")),
                            )
                        )
                    except ValueError:
                        malformed(f"column style range in {model.part}")
        elif name == "mergeCells":
            counts = 0
            for merge in elem:
                if local_name(merge.tag) == "mergeCell":
                    counts += 1
                    model.merges.append(
                        MergeDecl((*path, ("mergeCell", counts)), merge.get("ref", ""))
                    )
        elif name == "hyperlinks":
            counts = 0
            for link in elem:
                if local_name(link.tag) != "hyperlink":
                    continue
                counts += 1
                rel_id = next(
                    (v for k, v in link.attrib.items() if k.startswith("{") and k.endswith("}id")),
                    None,
                )
                model.hyperlinks.append(
                    Hyperlink(
                        (*path, ("hyperlink", counts)),
                        link.get("ref", ""),
                        rel_id,
                        link.get("location"),
                        link.get("display"),
                        link.get("tooltip"),
                    )
                )
        elif name == "conditionalFormatting":
            types: list[str] = []
            dxfs: list[int] = []
            formulas: list[str] = []
            extension = False
            for rule in elem:
                if local_name(rule.tag) == "extLst":
                    extension = True
                    continue
                if local_name(rule.tag) != "cfRule":
                    continue
                types.append(rule.get("type", ""))
                if rule.get("dxfId") is not None:
                    try:
                        dxfs.append(int(rule.get("dxfId", "")))
                    except ValueError:
                        malformed(f"conditional-format style in {model.part}")
                for node in rule:
                    node_name = local_name(node.tag)
                    if node_name == "formula":
                        formulas.append(node.text or "")
                    elif node_name == "extLst":
                        extension = True
                    elif node_name in ("colorScale", "dataBar", "iconSet"):
                        types.append(node_name)
            model.conditionals.append(
                ConditionalDecl(
                    path,
                    elem.get("sqref", ""),
                    tuple(types),
                    tuple(dxfs),
                    tuple(formulas),
                    extension,
                )
            )
        elif name == "dataValidations":
            for validation in elem:
                for node in validation:
                    if local_name(node.tag) in ("formula1", "formula2"):
                        model.validation_formulas.append(node.text or "")
        elif name == "tableParts":
            for part in elem:
                if local_name(part.tag) == "tablePart":
                    rel_id = next(
                        (
                            v
                            for k, v in part.attrib.items()
                            if k.startswith("{") and k.endswith("}id")
                        ),
                        None,
                    )
                    if rel_id is None:
                        malformed(f"table reference without a relationship in {model.part}")
                    model.table_rel_ids.append(rel_id)
        elif name == "autoFilter":
            model.auto_filter = elem.get("ref")
        elif name == "extLst":
            self._read_extensions(elem)
        elif name == "AlternateContent":
            model.alternate_content = True

    def _read_extensions(self, elem: Element) -> None:
        main = namespace(elem.tag)
        for node in elem.iter():
            node_name = local_name(node.tag)
            node_ns = namespace(node.tag)
            if node_ns != main and node_name not in ("ext", "extLst"):
                self.model.extension_names.add(node_name)
            if node_name in ("f", "formula", "formula1", "formula2") and node_ns != main:
                self.model.extension_formula_count += 1
            if node_name == "conditionalFormatting" and node_ns != main:
                for child in node:
                    if local_name(child.tag) == "sqref" and child.text:
                        self.model.extension_conditionals.append(ExtensionConditional(child.text))


def read_worksheet(package: OoxmlPackage, part: str, shared: list[RichText]) -> WorksheetModel:
    model = WorksheetModel(part)
    reader = _WorksheetReader(model, shared)
    package.stream_xml(part, reader.on_start, reader.on_end)
    return model


def checked_range(text: str, part: str, what: str) -> Rect:
    try:
        return parse_range(text)
    except CellReferenceError:
        malformed(f"{what} reference {text[:40]!r} in {part}")
