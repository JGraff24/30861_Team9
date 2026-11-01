"""Apply approved corrections to a private candidate package (specification sections 8 and 11).

Edits are targeted span replacements in the affected XML parts; every other ZIP entry is
copied with identical uncompressed bytes, in source order, with its source metadata.
"""

import io
import posixpath
import zipfile
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from app.audit.review import Correction
from app.workbooks.formulas import rewrite_sheet_references
from app.workbooks.inventory import STRICT_MAIN, WorkbookInventory
from app.workbooks.package import CONTENT_TYPES_PART
from app.workbooks.xmltext import (
    Node,
    NotEditable,
    XmlText,
    escape_attribute,
    escape_text,
    qualified,
)

TRANSITIONAL_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
TRANSITIONAL_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
STRICT_REL = "http://purl.oclc.org/ooxml/officeDocument/relationships"
PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
RELS_CONTENT_TYPE = "application/vnd.openxmlformats-package.relationships+xml"
TABLE_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"
DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
FORMULA_ELEMENTS = frozenset({"f", "formula", "formula1", "formula2"})


class EditFailed(Exception):
    """A planned correction could not be applied with exact preservation."""


@dataclass(frozen=True)
class PlannedEdit:
    correction: Correction
    value: Mapping[str, object]


@dataclass(frozen=True)
class NewTable:
    sheet_part: str
    part: str
    rel_id: str
    table_id: int
    name: str
    correction: Correction


def family(inventory: WorkbookInventory) -> tuple[str, str]:
    """(main namespace, relationship namespace) of the source package family."""
    if inventory.strict:
        return STRICT_MAIN, STRICT_REL
    return TRANSITIONAL_MAIN, TRANSITIONAL_REL


def plan_new_tables(inventory: WorkbookInventory, corrections: list[Correction]) -> list[NewTable]:
    """Deterministic ids, names, part paths and relationship ids for header_new edits."""
    used_ids = {
        int(table.table_id)
        for table in inventory.all_tables
        if table.table_id is not None and table.table_id.isdigit()
    }
    used_names = {
        name.casefold()
        for name in [table.name for table in inventory.all_tables]
        + [d.name for d in inventory.defined_names]
        if name
    }
    used_parts = {name.casefold() for name in inventory.package.entry_names}
    used_rels: dict[str, set[str]] = {
        model.info.part: set(model.relationships) for model in inventory.sheets
    }
    planned: list[NewTable] = []
    for correction in corrections:
        table_id = 1
        while table_id in used_ids:
            table_id += 1
        used_ids.add(table_id)
        base = f"A11yTable{table_id}"
        name, suffix = base, 0
        while name.casefold() in used_names:
            suffix += 1
            name = f"{base}_{suffix}"
        used_names.add(name.casefold())
        number = 1
        while f"xl/tables/table{number}.xml" in used_parts:
            number += 1
        part = f"xl/tables/table{number}.xml"
        used_parts.add(part)
        rels = used_rels.setdefault(correction.part, set())
        rel_number = 1
        while f"rId{rel_number}" in rels:
            rel_number += 1
        rel_id = f"rId{rel_number}"
        rels.add(rel_id)
        planned.append(NewTable(correction.part, part, rel_id, table_id, name, correction))
    return planned


def table_xml(table: NewTable, main: str) -> bytes:
    rect = table.correction.rect
    assert rect is not None
    columns = "".join(
        f'<tableColumn id="{index}" name="{escape_attribute(label)}"/>'
        for index, label in enumerate(table.correction.labels, start=1)
    )
    return (
        f'{DECLARATION}<table xmlns="{main}" id="{table.table_id}" name="{table.name}" '
        f'displayName="{table.name}" ref="{rect.canonical()}" headerRowCount="1" '
        f'totalsRowShown="0"><tableColumns count="{len(table.correction.labels)}">{columns}'
        "</tableColumns></table>"
    ).encode()


class _Session:
    def __init__(self, inventory: WorkbookInventory) -> None:
        self.inventory = inventory
        self.docs: dict[str, XmlText] = {}
        self.added: dict[str, bytes] = {}

    def doc(self, part: str) -> XmlText:
        stored = self.inventory.package.find(part)
        if stored is None:
            raise EditFailed(f"part {part} is missing")
        if stored not in self.docs:
            try:
                self.docs[stored] = XmlText(self.inventory.package.read_bytes(stored))
            except NotEditable as exc:
                raise EditFailed(str(exc)) from exc
        return self.docs[stored]


def _child(doc: XmlText, node: Node, local: str) -> Node | None:
    return next((child for child in doc.children(node) if child.local == local), None)


def _apply_alt(session: _Session, correction: Correction, value: Mapping[str, object]) -> None:
    doc = session.doc(correction.part)
    matches = [
        node
        for node in doc.nodes
        if node.local == "cNvPr" and node.attribute("id") == correction.object_id
    ]
    if len(matches) != 1:
        raise EditFailed("the object is not uniquely identified")
    doc.set_attribute(matches[0], "title", str(value["title"]))
    doc.set_attribute(matches[0], "descr", str(value["description"]))


def _apply_link(session: _Session, correction: Correction, value: Mapping[str, object]) -> None:
    assert correction.cell_path is not None and correction.link_path is not None
    doc = session.doc(correction.part)
    cell = doc.find(correction.cell_path)
    if cell.local != "c" or any(child.local not in ("v", "is") for child in doc.children(cell)):
        raise EditFailed("the hyperlink cell is not a simple text cell")
    head = doc.text[cell.start : cell.attrs_end]
    existing = cell.attributes.get("t")
    if existing is not None:
        start, end = existing[1] - cell.start, existing[2] - cell.start
        head = head[:start] + '"inlineStr"' + head[end:]
    else:
        head += ' t="inlineStr"'
    prefix = cell.prefix
    is_tag, t_tag = qualified(prefix, "is"), qualified(prefix, "t")
    text = escape_text(str(value["text"]))
    doc.replace_element(
        cell,
        f'{head}><{is_tag}><{t_tag} xml:space="preserve">{text}</{t_tag}>'
        f"</{is_tag}></{cell.qname}>",
    )
    link = doc.find(correction.link_path)
    if link.local != "hyperlink":
        raise EditFailed("the hyperlink declaration moved")
    if "display" in link.attributes:
        doc.set_attribute(link, "display", str(value["text"]))


def _apply_header_enable(session: _Session, correction: Correction) -> None:
    doc = session.doc(correction.part)
    if doc.root.local != "table" or doc.root.attribute("headerRowCount") != "0":
        raise EditFailed("the table header state changed")
    doc.set_attribute(doc.root, "headerRowCount", "1")


def _apply_new_tables(session: _Session, tables: list[NewTable]) -> None:
    inventory = session.inventory
    main, rel_ns = family(inventory)
    by_sheet: dict[str, list[NewTable]] = defaultdict(list)
    for table in tables:
        by_sheet[table.sheet_part].append(table)
        session.added[table.part] = table_xml(table, main)
    content = session.doc(CONTENT_TYPES_PART)
    overrides = "".join(
        f'<{qualified(content.root.prefix, "Override")} PartName="/{table.part}" '
        f'ContentType="{TABLE_CONTENT_TYPE}"/>'
        for table in tables
    )
    for sheet_part, sheet_tables in by_sheet.items():
        rels_part = posixpath.join(
            posixpath.dirname(sheet_part), "_rels", posixpath.basename(sheet_part) + ".rels"
        )
        folder = posixpath.dirname(sheet_part)
        relationships = "".join(
            f'<Relationship Id="{table.rel_id}" Type="{rel_ns}/table" '
            f'Target="{escape_attribute(posixpath.relpath(table.part, folder))}"/>'
            for table in sheet_tables
        )
        if inventory.package.find(rels_part) is not None:
            rels = session.doc(rels_part)
            if rels.root.local != "Relationships":
                raise EditFailed("unexpected relationship part")
            prefix = rels.root.prefix
            relationships = relationships.replace(
                "<Relationship ", f"<{qualified(prefix, 'Relationship')} "
            )
            rels.append_child(rels.root, relationships)
        else:
            session.added[rels_part] = (
                f'{DECLARATION}<Relationships xmlns="{PACKAGE_REL}">{relationships}</Relationships>'
            ).encode()
            if inventory.package.content_type(rels_part) is None:
                overrides += (
                    f'<{qualified(content.root.prefix, "Default")} Extension="rels" '
                    f'ContentType="{RELS_CONTENT_TYPE}"/>'
                )
        sheet = session.doc(sheet_part)
        root = sheet.root
        p = root.prefix
        r_prefix = sheet.prefix_for(root, rel_ns)
        declaration = "" if r_prefix else f' xmlns:r="{rel_ns}"'
        r_prefix = r_prefix or "r"
        parts = "".join(
            f'<{qualified(p, "tablePart")}{declaration} {r_prefix}:id="{table.rel_id}"/>'
            for table in sheet_tables
        )
        existing = _child(sheet, root, "tableParts")
        if existing is not None:
            count = len([child for child in sheet.children(existing) if child.local == "tablePart"])
            sheet.set_attribute(existing, "count", str(count + len(sheet_tables)))
            sheet.append_child(existing, parts)
        else:
            container = qualified(p, "tableParts")
            markup = f'<{container} count="{len(sheet_tables)}">{parts}</{container}>'
            extension = _child(sheet, root, "extLst")
            sheet.insert(extension.start if extension is not None else root.end, markup)
    content.append_child(content.root, overrides)


def _apply_renames(session: _Session, renames: list[tuple[Correction, str]]) -> None:
    inventory = session.inventory
    mapping = [(correction.sheet_name or "", new) for correction, new in renames]

    def rewrite(text: str) -> str:
        for old, new in mapping:
            try:
                text = rewrite_sheet_references(text, old, new).text
            except ValueError as exc:
                raise EditFailed(f"reference rewrite refused: {exc}") from exc
        return text

    workbook = session.doc(inventory.structure.workbook_part)
    root_name = workbook.root.local
    for correction, new in renames:
        node = workbook.find(
            ((root_name, 1), ("sheets", 1), ("sheet", correction.sheet_position or 0))
        )
        if node.local != "sheet" or node.attribute("name") != correction.sheet_name:
            raise EditFailed("the worksheet entry moved")
        workbook.set_attribute(node, "name", new)
    for node in workbook.nodes:
        if node.local == "definedName":
            original = workbook.text_content(node)
            updated = rewrite(original)
            if updated != original:
                workbook.replace_text(node, updated)
    parts = [model.info.part for model in inventory.sheets if model.worksheet is not None]
    parts.extend(inventory.chart_formulas)
    for part in parts:
        stored = inventory.package.find(part)
        if stored is None:
            raise EditFailed(f"part {part} is missing")
        # Sheet tokens are XML character data, not raw byte substrings. Numeric
        # character references and Unicode case folding can hide a relevant name
        # from a byte search. Parse every understood reference-bearing part.
        doc = session.doc(part)
        for node in doc.nodes:
            if node.local in FORMULA_ELEMENTS and not node.self_closing:
                original = doc.text_content(node)
                updated = rewrite(original)
                if updated != original:
                    doc.replace_text(node, updated)
            elif node.local == "hyperlink" and "location" in node.attributes:
                original = node.attribute("location") or ""
                updated = rewrite(original)
                if updated != original:
                    doc.set_attribute(node, "location", updated)


def apply_edits(data: bytes, inventory: WorkbookInventory, edits: list[PlannedEdit]) -> bytes:
    session = _Session(inventory)
    renames: list[tuple[Correction, str]] = []
    new_tables: list[Correction] = []
    try:
        for edit in edits:
            correction = edit.correction
            if correction.kind == "alt":
                _apply_alt(session, correction, edit.value)
            elif correction.kind == "link":
                _apply_link(session, correction, edit.value)
            elif correction.kind == "header_enable":
                _apply_header_enable(session, correction)
            elif correction.kind == "header_new":
                new_tables.append(correction)
            elif correction.kind == "rename":
                renames.append((correction, str(edit.value["name"])))
        if new_tables:
            _apply_new_tables(session, plan_new_tables(inventory, new_tables))
        if renames:
            _apply_renames(session, renames)
        replaced = {part: doc.render() for part, doc in session.docs.items() if doc.changed}
    except NotEditable as exc:
        raise EditFailed(str(exc)) from exc
    return rebuild_package(data, replaced, session.added)


def rebuild_package(data: bytes, replaced: dict[str, bytes], added: dict[str, bytes]) -> bytes:
    """Copy every entry in source order with its metadata; append new parts at the end."""
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, "w") as target:
        stamp = (1980, 1, 1, 0, 0, 0)
        for info in source.infolist():
            content = replaced.get(info.filename)
            if content is None:
                content = source.read(info.filename)
            copy = zipfile.ZipInfo(info.filename, info.date_time)
            copy.compress_type = info.compress_type
            copy.external_attr = info.external_attr
            copy.create_system = info.create_system
            copy.comment = info.comment
            target.writestr(copy, content)
            stamp = max(stamp, info.date_time)
        for name in sorted(added):
            entry = zipfile.ZipInfo(name, stamp)
            entry.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(entry, added[name])
    return output.getvalue()
