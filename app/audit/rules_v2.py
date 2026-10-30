"""Ruleset 2.0.0: six rules with review context and correction safety profiles.

Each rule returns Entry records that pair a finding with the owner's review context and,
for a supported correction, the exact plan the editor will apply. Classification is
REQUIRES_USER_INPUT only when the editor can apply the correction safely; everything else
is MANUAL_REVIEW with a limitation explaining why.
"""

import unicodedata
from collections import deque
from dataclasses import dataclass

from app.audit.color import contrast_ratio, is_large_text, is_nonneutral, passes
from app.audit.identity import object_target, worksheet_target, xml_target
from app.audit.objects import package_inventory, sheet_inventories
from app.audit.review import Correction, Entry, RuleOutput, display
from app.audit.rules import (
    WORKSHEET_NAME,
    RuleContext,
    alt_text_findings,
    is_generic_worksheet_name,
)
from app.contracts.models import FindingCategory, FindingClassification
from app.workbooks.cells import Cell, WorksheetModel, checked_range, malformed, ws_trim
from app.workbooks.formulas import parse_hyperlink
from app.workbooks.inventory import SheetModel, TableInfo, WorkbookInventory
from app.workbooks.refs import (
    CellReferenceError,
    Rect,
    RectIndex,
    cell_ref,
    parse_range,
    parse_sqref,
)
from app.workbooks.styles import RGB, Resolved, rgb_hex
from app.workbooks.xmltext import editable_encoding

TABLE_HEADER = "TABLE_HEADER"
MERGED_CELLS = "MERGED_CELLS"
LINK_TEXT = "LINK_TEXT"
COLOR_CONTRAST = "COLOR_CONTRAST"
WORKBOOK_LEVEL = "__WORKBOOK__"
AUDITED_SHEET_KINDS = frozenset({"worksheet", "chartsheet"})
REQUIRES = FindingClassification.REQUIRES_USER_INPUT
MANUAL = FindingClassification.MANUAL_REVIEW
AMBIGUOUS_LABELS = frozenset(
    {"click here", "here", "link", "more", "read more", "learn more", "website", "url"}
)
ADDRESS_PREFIXES = ("http://", "https://", "www.", "mailto:")
MAX_HEADER_LABEL = 255
SIGNED_LIMITATION = (
    "The workbook is digitally signed; editing it would invalidate the signature, so all "
    "corrections require manual review."
)


class Editability:
    """Raw-part encoding checks, cached per part (edits need exact UTF-8 spans)."""

    def __init__(self, inventory: WorkbookInventory) -> None:
        self.inventory = inventory
        self.cache: dict[str, bool] = {}

    def part(self, name: str | None) -> bool:
        if name is None:
            return True
        if name not in self.cache:
            stored = self.inventory.package.find(name)
            self.cache[name] = stored is not None and editable_encoding(
                self.inventory.package.read_bytes(stored)
            )
        return self.cache[name]


def _manual(entry: Entry, reason: str) -> Entry:
    entry.finding = entry.finding.model_copy(update={"classification": MANUAL})
    entry.reason = reason
    entry.current = None
    entry.correction = None
    return entry


# -- ALT_TEXT ------------------------------------------------------------------------


def alt_text(context: RuleContext, inventory: WorkbookInventory, edit: Editability) -> RuleOutput:
    output = RuleOutput()
    package = inventory.package
    groups = [
        (sheet.name, item)
        for sheet in inventory.structure.sheets
        if sheet.kind in AUDITED_SHEET_KINDS
        for item in sheet_inventories(package, sheet)
    ]
    groups.append((WORKBOOK_LEVEL, package_inventory(package)))
    for worksheet, drawing in groups:
        findings, limitations = alt_text_findings(context, worksheet, drawing)
        output.limitations.update(limitations)
        objects = {object_target(obj.part, obj.object_id): obj for obj in drawing.objects}
        for finding in findings:
            entry = Entry(finding)
            obj = objects.get(finding.target)
            if finding.classification != REQUIRES or obj is None:
                entry.finding = finding.model_copy(update={"classification": MANUAL})
                entry.reason = (
                    "This object must be reviewed manually; alternative text cannot be "
                    "corrected safely for this representation."
                )
                output.entries.append(entry)
                continue
            blocker = None
            if inventory.signed:
                blocker = SIGNED_LIMITATION
            elif len(inventory.drawing_owners.get(obj.part, [])) > 1:
                blocker = (
                    f"Drawing part {obj.part} is shared by several sheets; its objects need "
                    "manual alternative-text review."
                )
            elif not edit.part(obj.part):
                blocker = f"Drawing part {obj.part} is not UTF-8 encoded and is not edited."
            if blocker is not None:
                output.limitations.add(blocker)
                output.entries.append(
                    _manual(entry, "Edit this object's alternative text manually.")
                )
                continue
            entry.current = {"title": obj.title, "description": obj.descr}
            entry.reason = (
                "Provide both an alternative text title and description; existing values are "
                "shown for confirmation."
            )
            entry.correction = Correction("alt", obj.part, object_id=obj.object_id)
            output.entries.append(entry)
    return output


# -- WORKSHEET_NAME ------------------------------------------------------------------


def worksheet_names(
    context: RuleContext, inventory: WorkbookInventory, edit: Editability
) -> RuleOutput:
    output = RuleOutput()
    blocker = None
    if inventory.signed:
        blocker = SIGNED_LIMITATION
    elif inventory.rename_blockers:
        shown = "; ".join(inventory.rename_blockers[:3])
        more = "; and other reference-bearing content" if len(inventory.rename_blockers) > 3 else ""
        blocker = display(
            f"Worksheet renames need manual review because the workbook contains {shown}{more}.",
            480,
        )
    elif not edit.part(inventory.structure.workbook_part):
        blocker = "The workbook part is not UTF-8 encoded; worksheet renames are not edited."
    for model in inventory.sheets:
        sheet = model.info
        if sheet.kind not in AUDITED_SHEET_KINDS or not is_generic_worksheet_name(sheet.name):
            continue
        finding = context.finding(
            WORKSHEET_NAME,
            FindingCategory.WORKSHEET_NAME,
            sheet.name,
            worksheet_target(sheet.part, sheet.sheet_id),
            REQUIRES,
            f'Worksheet "{display(sheet.name)}" uses a generic default name.',
            "Rename the worksheet with a descriptive name that identifies its content.",
        )
        entry = Entry(finding)
        if blocker is not None:
            output.limitations.add(blocker)
            output.entries.append(
                _manual(entry, "Rename this worksheet manually after reviewing its references.")
            )
            continue
        entry.current = {"name": sheet.name}
        entry.reason = "Provide a descriptive worksheet name; references to this sheet are updated."
        entry.correction = Correction(
            "rename",
            inventory.structure.workbook_part,
            sheet_name=sheet.name,
            sheet_position=model.position,
        )
        output.entries.append(entry)
    return output


# -- shared helpers -------------------------------------------------------------------


def _merge_rects(model: SheetModel) -> list[Rect]:
    assert model.worksheet is not None
    rects: list[Rect] = []
    for merge in model.worksheet.merges:
        rect = checked_range(merge.ref, model.info.part, "merged range")
        if rect.area > 1 and rect not in rects:
            rects.append(rect)
    return rects


def _populated(worksheet: WorksheetModel) -> dict[tuple[int, int], Cell]:
    return {key: cell for key, cell in worksheet.cells.items() if cell.populated}


def _rect_has(rect: Rect, cells: dict[tuple[int, int], Cell]) -> bool:
    if rect.area <= len(cells):
        return any(
            (row, col) in cells
            for row in range(rect.top, rect.bottom + 1)
            for col in range(rect.left, rect.right + 1)
        )
    return any(rect.contains(row, col) for row, col in cells)


def _casefold_key(text: str) -> str:
    return ws_trim(text).casefold()


def _has_control(text: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in text)


# -- MERGED_CELLS --------------------------------------------------------------------


def merged_cells(context: RuleContext, inventory: WorkbookInventory) -> RuleOutput:
    output = RuleOutput()
    for model in inventory.sheets:
        if model.worksheet is None:
            continue
        rects = _merge_rects(model)
        populated = _populated(model.worksheet)
        overlapping: set[Rect] = set()
        for index, rect in enumerate(rects):
            for other in rects[index + 1 :]:
                if rect.intersects(other):
                    overlapping.update((rect, other))
        if overlapping:
            output.limitations.add(
                f"Worksheet part {model.info.part} has overlapping merged ranges; they need "
                "manual review."
            )
        for rect in rects:
            conflict = rect in overlapping
            if not conflict and not _rect_has(rect, populated):
                continue
            message = (
                f"Merged range {rect.canonical()} overlaps another merge declaration."
                if conflict
                else f"Merged range {rect.canonical()} contains content and may impede "
                "navigation with assistive technology."
            )
            finding = context.finding(
                MERGED_CELLS,
                FindingCategory.MERGED_CELLS,
                model.info.name,
                f"{model.info.part}#merge={rect.canonical()}",
                MANUAL,
                message,
                "Confirm the merge does not hinder navigation or reading order; if it does, "
                "unmerge the cells and keep each value in its own cell.",
            )
            output.entries.append(
                Entry(
                    finding,
                    reason=(
                        "Merged cells are reviewed manually; they are never unmerged automatically."
                    ),
                )
            )
    return output


# -- TABLE_HEADER ----------------------------------------------------------------------


def _structured_reference(inventory: WorkbookInventory, table_name: str | None) -> bool:
    if not table_name:
        return False
    marker = table_name.casefold()
    texts = [name.text for name in inventory.defined_names]
    for model in inventory.sheets:
        if model.worksheet is not None:
            texts.extend(
                cell.formula for cell in model.worksheet.cells.values() if cell.formula is not None
            )
    return any(marker in text.casefold() for text in texts)


def _table_problems(
    table: TableInfo, model: SheetModel, merges: list[Rect], others: list[TableInfo]
) -> list[str]:
    assert model.worksheet is not None
    rect = table.rect
    problems: list[str] = []
    if len(table.columns) != rect.cols:
        problems.append("its column metadata does not match its width")
    labels: list[str] = []
    for offset in range(rect.cols):
        cell = model.worksheet.cells.get((rect.top, rect.left + offset))
        if cell is None or not cell.populated:
            problems.append("a header cell is blank")
            continue
        if cell.formula is not None:
            problems.append("a header cell is formula-based")
            continue
        if cell.cell_type not in ("s", "inlineStr") or cell.text is None:
            problems.append("a header cell is not plain text")
            continue
        labels.append(cell.text)
        if offset < len(table.columns) and cell.text != table.columns[offset].name:
            problems.append("a header cell differs from its column metadata")
    keys = [_casefold_key(label) for label in labels]
    if len(set(keys)) != len(keys):
        problems.append("header labels are duplicated")
    header = Rect(rect.top, rect.left, rect.top, rect.right)
    if any(merge.intersects(header) for merge in merges):
        problems.append("the header row contains merged cells")
    if any(other is not table and other.rect.intersects(rect) for other in others):
        problems.append("it overlaps another table")
    return sorted(set(problems), key=problems.index)


def _enable_safe(
    table: TableInfo,
    model: SheetModel,
    merges: list[Rect],
    inventory: WorkbookInventory,
    edit: Editability,
) -> bool:
    assert model.worksheet is not None
    rect = table.rect
    if (
        inventory.signed
        or table.owners != 1
        or rect.rows < 2
        or len(table.columns) != rect.cols
        or table.totals_count != 0
        or table.table_type != "worksheet"
        or table.has_connection
        or table.has_extension
        or any(merge.intersects(rect) for merge in merges)
        or any(other is not table and other.rect.intersects(rect) for other in model.tables)
        or _structured_reference(inventory, table.name)
        or not edit.part(table.part)
    ):
        return False
    names = [column.name for column in table.columns]
    if len({_casefold_key(name) for name in names}) != len(names):
        return False
    for offset, name in enumerate(names):
        cell = model.worksheet.cells.get((rect.top, rect.left + offset))
        if cell is None or not cell.plain_string or cell.text != name or not ws_trim(name):
            return False
    return True


def _components(cells: set[tuple[int, int]]) -> list[set[tuple[int, int]]]:
    remaining = set(cells)
    components: list[set[tuple[int, int]]] = []
    for start in sorted(cells):
        if start not in remaining:
            continue
        remaining.discard(start)
        component = {start}
        queue = deque([start])
        while queue:
            row, col = queue.popleft()
            for neighbor in ((row - 1, col), (row + 1, col), (row, col - 1), (row, col + 1)):
                if neighbor in remaining:
                    remaining.discard(neighbor)
                    component.add(neighbor)
                    queue.append(neighbor)
        components.append(component)
    return components


@dataclass(frozen=True)
class _Candidate:
    rect: Rect
    labels: tuple[str, ...] | None  # set only for a safe dense candidate


def _candidate(
    component: set[tuple[int, int]],
    model: SheetModel,
    merges: list[Rect],
    inventory: WorkbookInventory,
    edit: Editability,
) -> _Candidate:
    assert model.worksheet is not None
    rows = [row for row, _ in component]
    cols = [col for _, col in component]
    rect = Rect(min(rows), min(cols), max(rows), max(cols))
    unsafe = _Candidate(rect, None)
    if len(component) != rect.area:
        return unsafe
    worksheet = model.worksheet
    labels: list[str] = []
    for col in range(rect.left, rect.right + 1):
        cell = worksheet.cells[(rect.top, col)]
        text = cell.text or ""
        if not cell.plain_string or not ws_trim(text) or len(text) > MAX_HEADER_LABEL:
            return unsafe
        if _has_control(text):
            return unsafe
        labels.append(text)
    if len({_casefold_key(label) for label in labels}) != len(labels):
        return unsafe
    if any(merge.intersects(rect) for merge in merges):
        return unsafe
    if any(table.rect.intersects(rect) for table in model.tables):
        return unsafe
    if worksheet.auto_filter is not None:
        try:
            if checked_range(worksheet.auto_filter, model.info.part, "filter").intersects(rect):
                return unsafe
        except CellReferenceError:
            return unsafe
    if any(worksheet.cells[key].has_value_metadata for key in component):
        return unsafe
    rels_part = _rels_part(model.info.part)
    if (
        inventory.signed
        or worksheet.alternate_content
        or not edit.part(model.info.part)
        or not edit.part("[Content_Types].xml")
        or (inventory.package.find(rels_part) is not None and not edit.part(rels_part))
    ):
        return unsafe
    return _Candidate(rect, tuple(labels))


def _rels_part(part: str) -> str:
    folder, _, name = part.rpartition("/")
    return f"{folder}/_rels/{name}.rels" if folder else f"_rels/{name}.rels"


def table_headers(
    context: RuleContext, inventory: WorkbookInventory, edit: Editability
) -> RuleOutput:
    output = RuleOutput()
    tables = inventory.all_tables
    id_counts: dict[str, int] = {}
    for table in tables:
        if table.table_id:
            id_counts[table.table_id] = id_counts.get(table.table_id, 0) + 1
    small_components = False
    for model in inventory.sheets:
        worksheet = model.worksheet
        if worksheet is None:
            continue
        merges = _merge_rects(model)
        for table in model.tables:
            entry = _formal_table_entry(context, inventory, edit, model, table, merges, id_counts)
            if entry is not None:
                output.entries.append(entry)
            if not table.table_id or id_counts.get(table.table_id, 0) != 1:
                output.limitations.add(
                    f"Table part {table.part} has a missing or duplicate table id; it needs "
                    "manual review."
                )
        covered = RectIndex(table.rect for table in model.tables)
        populated = {
            key
            for key, cell in worksheet.cells.items()
            if cell.populated and not covered.contains(*key)
        }
        for component in _components(populated):
            rows = {row for row, _ in component}
            cols = {col for _, col in component}
            if len(rows) < 2 or len(cols) < 2:
                small_components = True
                continue
            candidate = _candidate(component, model, merges, inventory, edit)
            target = f"{model.info.part}#range={candidate.rect.canonical()}"
            if candidate.labels is not None:
                finding = context.finding(
                    TABLE_HEADER,
                    FindingCategory.TABLE_STRUCTURE,
                    model.info.name,
                    target,
                    REQUIRES,
                    f"Range {candidate.rect.canonical()} looks like a data table without "
                    "declared header cells.",
                    "Confirm whether the first row of this range holds the column headers; "
                    "if so, it is marked as an Excel table header row.",
                )
                output.entries.append(
                    Entry(
                        finding,
                        {"headerRow": candidate.rect.top},
                        "Confirm the first row of the reported range as its header row.",
                        Correction(
                            "header_new",
                            model.info.part,
                            rect=candidate.rect,
                            labels=candidate.labels,
                        ),
                    )
                )
            else:
                output.limitations.add(
                    "Some populated ranges could not be established safely as tables; their "
                    "logical structure needs manual review."
                )
                finding = context.finding(
                    TABLE_HEADER,
                    FindingCategory.TABLE_STRUCTURE,
                    model.info.name,
                    target,
                    MANUAL,
                    f"Range {candidate.rect.canonical()} may be a data table, but its header "
                    "row could not be established safely.",
                    "Review whether this range is a table. If it is, give it one row of "
                    "distinct text headers and format it as an Excel table.",
                )
                output.entries.append(
                    Entry(finding, reason="Table structure for this range needs human judgment.")
                )
    if small_components:
        output.limitations.add(
            "Single cells, one-row ranges and one-column lists are not inferred as tables "
            "under this screening profile."
        )
    return output


def _formal_table_entry(
    context: RuleContext,
    inventory: WorkbookInventory,
    edit: Editability,
    model: SheetModel,
    table: TableInfo,
    merges: list[Rect],
    id_counts: dict[str, int],
) -> Entry | None:
    name = display(table.name or "table")
    if not table.table_id or id_counts.get(table.table_id, 0) != 1:
        finding = context.finding(
            TABLE_HEADER,
            FindingCategory.TABLE_STRUCTURE,
            model.info.name,
            f"{table.part}#xml=/{table.root_name}[1]",
            MANUAL,
            f'Table "{name}" has a missing or duplicate table id and could not be assessed.',
            "Review this table's header row and structure manually.",
        )
        return Entry(finding, reason="Tables without a unique id are reviewed manually.")
    target = f"{table.part}#tableId={table.table_id}"
    header_rows = table.header_rows
    if header_rows == 0:
        safe = _enable_safe(table, model, merges, inventory, edit)
        finding = context.finding(
            TABLE_HEADER,
            FindingCategory.TABLE_STRUCTURE,
            model.info.name,
            target,
            REQUIRES if safe else MANUAL,
            f'Table "{name}" has its header row turned off.',
            "Turn on the table's header row so each column has a programmatic header.",
        )
        if safe:
            return Entry(
                finding,
                {"headerRow": table.rect.top},
                "Confirm the table's first row, which already matches its column names, as "
                "its header row.",
                Correction("header_enable", table.part, rect=table.rect),
            )
        return Entry(
            finding,
            reason="Enabling this table's header would change its data; review manually.",
        )
    if header_rows != 1:
        finding = context.finding(
            TABLE_HEADER,
            FindingCategory.TABLE_STRUCTURE,
            model.info.name,
            target,
            MANUAL,
            f'Table "{name}" declares an unsupported number of header rows.',
            "Review the table so it has exactly one row of column headers.",
        )
        return Entry(finding, reason="Unsupported header layouts are reviewed manually.")
    problems = _table_problems(table, model, merges, model.tables)
    if not problems:
        return None
    finding = context.finding(
        TABLE_HEADER,
        FindingCategory.TABLE_STRUCTURE,
        model.info.name,
        target,
        MANUAL,
        display(f'Table "{name}" header row needs review: {"; ".join(problems)}.', 480),
        "Give the table one row of distinct, nonblank text headers that match its columns.",
    )
    return Entry(finding, reason="Header problems in an existing table are reviewed manually.")


# -- LINK_TEXT -------------------------------------------------------------------------


def _normalized(text: str) -> str:
    collapsed = " ".join(part for part in _split_white(ws_trim(text)) if part).casefold()
    if collapsed[-1:] in (".", "!", "?"):
        collapsed = collapsed[:-1]
    return collapsed


def _split_white(text: str) -> list[str]:
    from app.workbooks.cells import WHITE_SPACE

    parts: list[str] = []
    current: list[str] = []
    for character in text:
        if character in WHITE_SPACE:
            parts.append("".join(current))
            current = []
        else:
            current.append(character)
    parts.append("".join(current))
    return parts


def ambiguous_label(label: str, destination: str | None) -> bool:
    if not ws_trim(label):
        return True
    normalized = _normalized(label)
    if normalized in AMBIGUOUS_LABELS or normalized.startswith(ADDRESS_PREFIXES):
        return True
    if destination is not None and ws_trim(destination):
        if ws_trim(label) == ws_trim(destination) or normalized == _normalized(destination):
            return True
    return False


def hyperlinks(context: RuleContext, inventory: WorkbookInventory, edit: Editability) -> RuleOutput:
    output = RuleOutput()
    if inventory.drawing_hyperlinks:
        output.limitations.add(
            "Hyperlinks attached to drawing objects are not screened for link text; review "
            "them manually."
        )
    for model in inventory.sheets:
        worksheet = model.worksheet
        if worksheet is None:
            continue
        part = model.info.part
        merges = _merge_rects(model)
        parsed: list[tuple[int, Rect | None]] = []
        counts: dict[tuple[int, int], int] = {}
        for index, link in enumerate(worksheet.hyperlinks):
            try:
                rect = parse_range(link.ref) if link.ref else None
            except CellReferenceError:
                rect = None  # a malformed link is a reviewable representation, not a failure
            parsed.append((index, rect))
            if rect is not None and rect.area == 1:
                counts[(rect.top, rect.left)] = counts.get((rect.top, rect.left), 0) + 1
        for index, rect in parsed:
            link = worksheet.hyperlinks[index]
            xml = xml_target(part, link.path)

            def manual(
                message: str,
                limitation: str | None = None,
                *,
                target: str = xml,
                sheet_name: str = model.info.name,
            ) -> None:
                if limitation:
                    output.limitations.add(limitation)
                finding = context.finding(
                    LINK_TEXT,
                    FindingCategory.LINK_TEXT,
                    sheet_name,
                    target,
                    MANUAL,
                    message,
                    "Make sure the visible link text describes the destination; edit this "
                    "link manually.",
                )
                output.entries.append(
                    Entry(finding, reason="This hyperlink representation is reviewed manually.")
                )

            if rect is None:
                manual(
                    "A hyperlink has a missing or malformed cell reference.",
                    f"Worksheet part {part} has a malformed hyperlink declaration.",
                )
                continue
            if rect.area > 1:
                manual(f"Hyperlink over range {rect.canonical()} spans several cells.")
                continue
            if counts[(rect.top, rect.left)] > 1:
                manual(f"Cell {cell_ref(rect.top, rect.left)} has several hyperlink declarations.")
                continue
            destination = link.location
            if link.rel_id is not None:
                rel = model.relationships.get(link.rel_id)
                if rel is None:
                    manual(
                        f"Hyperlink at {cell_ref(rect.top, rect.left)} has an unresolved "
                        "destination.",
                        f"Worksheet part {part} has a hyperlink without a resolvable relationship.",
                    )
                    continue
                destination = rel.target
            cell = worksheet.cells.get((rect.top, rect.left))
            label = (cell.text or "") if cell is not None else ""
            if not ambiguous_label(label, destination):
                continue
            ref = cell_ref(rect.top, rect.left)
            shown = display(label, 60) if ws_trim(label) else ""
            message = (
                f'Hyperlink text "{shown}" at {ref} does not describe its destination.'
                if shown
                else f"Hyperlink at {ref} has no visible link text."
            )
            safe = (
                cell is not None
                and cell.formula is None
                and not cell.has_value_metadata
                and set(cell.child_names) <= {"v", "is"}
                and (
                    cell.cell_type in ("s", "inlineStr")
                    and cell.rich is not None
                    and not cell.rich.has_runs
                    or cell.cell_type in ("n", "inlineStr")
                    and cell.value is None
                    and cell.rich is None
                )
                and not any(merge.contains(rect.top, rect.left) for merge in merges)
                and not inventory.signed
                and edit.part(part)
            )
            if not safe:
                manual(message)
                continue
            assert cell is not None
            finding = context.finding(
                LINK_TEXT,
                FindingCategory.LINK_TEXT,
                model.info.name,
                f"{part}#cell={ref}",
                REQUIRES,
                message,
                "Replace the link text with words that describe where the link goes.",
            )
            output.entries.append(
                Entry(
                    finding,
                    {"text": label},
                    "Provide descriptive link text; the destination is not changed.",
                    Correction(
                        "link",
                        part,
                        cell=(cell.row, cell.col),
                        cell_path=cell.path,
                        link_path=link.path,
                    ),
                )
            )
        for cell in sorted(worksheet.cells.values(), key=lambda item: (item.row, item.col)):
            if cell.formula is None:
                continue
            call = parse_hyperlink(cell.formula)
            if call is None:
                continue
            target = xml_target(part, cell.path)
            if not call.literal or call.label is None:
                output.limitations.add(
                    "HYPERLINK formulas with computed or localized arguments were not screened "
                    "for link text; review them manually."
                )
                message = f"HYPERLINK formula at {cell.ref} has text that could not be screened."
            elif ambiguous_label(call.label, call.destination):
                shown = display(call.label, 60)
                message = (
                    f'HYPERLINK formula text "{shown}" at {cell.ref} does not describe its '
                    "destination."
                    if ws_trim(call.label)
                    else f"HYPERLINK formula at {cell.ref} has no visible link text."
                )
            else:
                continue
            finding = context.finding(
                LINK_TEXT,
                FindingCategory.LINK_TEXT,
                model.info.name,
                target,
                MANUAL,
                message,
                "Edit the formula so its link text describes the destination; formula-based "
                "links are never modified automatically.",
            )
            output.entries.append(
                Entry(finding, reason="Formula-based hyperlinks are reviewed manually.")
            )
    return output


# -- COLOR_CONTRAST ------------------------------------------------------------------


@dataclass(frozen=True)
class _Rendering:
    font: RGB | None
    fill: RGB | None
    large: bool
    unsupported: str | None
    defaults: bool  # missing fill or font color was screened as white/black


def _style_index(cell: Cell, worksheet: WorksheetModel) -> int:
    if cell.style is not None:
        return cell.style
    if cell.row in worksheet.row_styles:
        return worksheet.row_styles[cell.row]
    for first, last, style in worksheet.column_styles:
        if first <= cell.col <= last:
            return style
    return 0


def _render(inventory: WorkbookInventory, index: int) -> _Rendering:
    styles = inventory.styles
    if not 0 <= index < len(styles.cell_formats):
        return _Rendering(None, None, False, "an undefined cell style", False)
    cell_format = styles.cell_formats[index]
    if cell_format.ambiguous is not None:
        return _Rendering(None, None, False, cell_format.ambiguous, False)
    font_id = cell_format.font_id if cell_format.font_id is not None else 0
    fill_id = cell_format.fill_id if cell_format.fill_id is not None else 0
    if not 0 <= font_id < len(styles.fonts) or not 0 <= fill_id < len(styles.fills):
        return _Rendering(None, None, False, "an undefined font or fill", False)
    font = styles.fonts[font_id]
    fill = styles.fills[fill_id]
    if not font.size_valid:
        return _Rendering(None, None, False, "an unreadable font size", False)
    defaults = False
    font_rgb: RGB | None
    if font.color is None:
        font_rgb, defaults = (0, 0, 0), True
    else:
        resolved: Resolved = styles.resolve(font.color)
        if resolved.rgb is None:
            return _Rendering(None, None, False, resolved.reason, False)
        font_rgb = resolved.rgb
    fill_rgb: RGB | None
    if fill.kind == "none":
        fill_rgb, defaults = (255, 255, 255), True
    elif fill.kind == "solid":
        if fill.color is None:
            return _Rendering(None, None, False, "a solid fill without a color", False)
        resolved = styles.resolve(fill.color)
        if resolved.rgb is None:
            return _Rendering(None, None, False, resolved.reason, False)
        fill_rgb = resolved.rgb
    else:
        return _Rendering(None, None, False, f"a {fill.kind} fill", False)
    return _Rendering(font_rgb, fill_rgb, is_large_text(font.size, font.bold), None, defaults)


def color_contrast(context: RuleContext, inventory: WorkbookInventory) -> RuleOutput:
    output = RuleOutput()
    cache: dict[int, _Rendering] = {}
    for model in inventory.sheets:
        worksheet = model.worksheet
        if worksheet is None:
            continue
        part = model.info.part
        regions: list[Rect] = []
        for declaration in worksheet.conditionals:
            visual = declaration.has_extension or any(
                kind in ("colorScale", "dataBar", "iconSet") for kind in declaration.rule_types
            )
            for dxf in declaration.dxf_ids:
                if not 0 <= dxf < len(inventory.styles.differential):
                    visual = True
                elif inventory.styles.differential[dxf].changes_color:
                    visual = True
            if visual:
                regions.extend(_sqref(declaration.sqref, part))
        for extension in worksheet.extension_conditionals:
            regions.extend(_sqref(extension.sqref, part))
        unique: list[Rect] = []
        for rect in regions:
            if rect not in unique:
                unique.append(rect)
        for rect in unique:
            finding = context.finding(
                COLOR_CONTRAST,
                FindingCategory.COLOR_OR_CONTRAST,
                model.info.name,
                f"{part}#conditional={rect.canonical()}",
                MANUAL,
                f"Conditional formatting over {rect.canonical()} changes colors or adds visual "
                "indicators that may carry meaning.",
                "Make sure the information shown by conditional colors or icons is also "
                "available as text, and check the contrast of every formatted state.",
            )
            output.entries.append(
                Entry(finding, reason="Conditional formatting is reviewed manually.")
            )
        styled_tables = RectIndex(table.rect for table in model.tables if table.style_name)
        conditional = RectIndex(unique)
        for cell in sorted(worksheet.cells.values(), key=lambda item: (item.row, item.col)):
            if not cell.populated or conditional.contains(cell.row, cell.col):
                continue
            entry = _cell_entry(context, inventory, model, cell, styled_tables, cache, output)
            if entry is not None:
                output.entries.append(entry)
    return output


def _sqref(text: str, part: str) -> list[Rect]:
    try:
        return parse_sqref(text)
    except CellReferenceError:
        malformed(f"conditional-format range {text[:40]!r} in {part}")


def _cell_entry(
    context: RuleContext,
    inventory: WorkbookInventory,
    model: SheetModel,
    cell: Cell,
    styled_tables: RectIndex,
    cache: dict[int, _Rendering],
    output: RuleOutput,
) -> Entry | None:
    assert model.worksheet is not None
    index = _style_index(cell, model.worksheet)
    if index not in cache:
        cache[index] = _render(inventory, index)
    rendering = cache[index]
    reason = rendering.unsupported
    if reason is None and styled_tables.contains(cell.row, cell.col):
        reason = "a table style overlay"
    if reason is None and cell.rich is not None and cell.rich.styled_runs:
        reason = "rich text with mixed styling"
    target = f"{model.info.part}#cell={cell.ref}"
    recommendation = (
        "Make sure text contrasts sufficiently with its background, and that any meaning "
        "shown by color is also given in text or structure."
    )
    if reason is not None:
        output.limitations.add(
            f"Cell colors using {reason} were not resolved; those cells need manual contrast "
            "review."
        )
        finding = context.finding(
            COLOR_CONTRAST,
            FindingCategory.COLOR_OR_CONTRAST,
            model.info.name,
            target,
            MANUAL,
            f"Cell {cell.ref} uses {reason}, so its contrast could not be calculated.",
            recommendation,
        )
        return Entry(finding, reason="Color rendering for this cell is reviewed manually.")
    assert rendering.font is not None and rendering.fill is not None
    if rendering.defaults:
        output.limitations.add(
            "Cells without a fill were screened as white, and text without a font color as black."
        )
    nonneutral = is_nonneutral(rendering.font) or is_nonneutral(rendering.fill)
    no_text = cell.formula is not None and not cell.value
    problems: list[str] = []
    if no_text:
        if not nonneutral:
            return None
        output.limitations.add(
            "Formula results are not calculated; formula cells without cached text were "
            "screened for color use only."
        )
    else:
        ratio = contrast_ratio(rendering.font, rendering.fill)
        if not passes(ratio, rendering.large):
            minimum = "3:1" if rendering.large else "4.5:1"
            problems.append(f"text contrast is {ratio:.2f}:1, below {minimum}")
    if nonneutral:
        problems.append(
            f"it uses color (text #{rgb_hex(rendering.font)} on #{rgb_hex(rendering.fill)}) that "
            "may convey meaning"
        )
    if not problems:
        return None
    finding = context.finding(
        COLOR_CONTRAST,
        FindingCategory.COLOR_OR_CONTRAST,
        model.info.name,
        target,
        MANUAL,
        f"Cell {cell.ref}: {' and '.join(problems)}.",
        recommendation,
    )
    return Entry(
        finding,
        reason="Color and contrast are reviewed manually; colors are never changed automatically.",
    )


def analyze(context: RuleContext, inventory: WorkbookInventory) -> tuple[list[Entry], list[str]]:
    edit = Editability(inventory)
    output = RuleOutput()
    output.extend(alt_text(context, inventory, edit))
    output.extend(worksheet_names(context, inventory, edit))
    output.extend(table_headers(context, inventory, edit))
    output.extend(merged_cells(context, inventory))
    output.extend(hyperlinks(context, inventory, edit))
    output.extend(color_contrast(context, inventory))
    entries = output.entries
    entries.sort(
        key=lambda entry: (entry.finding.ruleId, entry.finding.worksheet, entry.finding.target)
    )
    return entries, sorted(output.limitations)
