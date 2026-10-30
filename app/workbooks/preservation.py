"""Independent preservation comparison (specification section 11).

The editor works on raw text spans; this verifier re-reads both packages with the
hardened tree parser and rebuilds the expected result from the source tree and the
explicit approved-change allowlist. Every untouched entry must have byte-identical
uncompressed contents; every changed XML part must equal its expected tree exactly.
"""

import io
import re
import zipfile
from collections.abc import Callable
from xml.etree.ElementTree import Element, SubElement

from app.workbooks.editor import (
    RELS_CONTENT_TYPE,
    TABLE_CONTENT_TYPE,
    NewTable,
    PlannedEdit,
    family,
    plan_new_tables,
)
from app.workbooks.formulas import quote_sheet, scan_formula
from app.workbooks.inventory import WorkbookInventory
from app.workbooks.package import CONTENT_TYPES_PART
from app.workbooks.xmlsafe import local_name, namespace, parse_document
from app.workbooks.xmltext import XML_NS

XmlPath = tuple[tuple[str, int], ...]
_DECLARATIONS = re.compile(rb"xmlns(?::[A-Za-z0-9_.\-]+)?\s*=\s*(\"[^\"]*\"|'[^']*')")
FORMULA_ELEMENTS = frozenset({"f", "formula", "formula1", "formula2", "definedName"})


class PreservationError(Exception):
    """The candidate differs from the source beyond the approved changes."""


def _parse(data: bytes) -> Element:
    return parse_document(io.BytesIO(data))


def find_path(root: Element, path: XmlPath) -> Element:
    if not path or local_name(root.tag) != path[0][0]:
        raise PreservationError("approved target path does not resolve")
    current = root
    for name, index in path[1:]:
        matches = [child for child in current if local_name(child.tag) == name]
        if len(matches) < index:
            raise PreservationError("approved target path does not resolve")
        current = matches[index - 1]
    return current


def _norm(text: str | None) -> str:
    return text or ""


def rename_equivalent(source: str, candidate: str, mapping: dict[str, str]) -> bool:
    """Same formula except single-sheet prefixes naming a renamed sheet."""
    if source == candidate:
        return True
    before, after = scan_formula(source), scan_formula(candidate)
    if before.unsupported or after.unsupported:
        return False
    if len(before.sheet_tokens) != len(after.sheet_tokens):
        return False
    cursor_a = cursor_b = 0
    for token_a, token_b in zip(before.sheet_tokens, after.sheet_tokens, strict=True):
        if source[cursor_a : token_a.start] != candidate[cursor_b : token_b.start]:
            return False
        raw_a = source[token_a.start : token_a.end]
        raw_b = candidate[token_b.start : token_b.end]
        renamed = (
            len(token_a.names) == 1
            and not token_a.external
            and token_a.names[0].casefold() in mapping
        )
        expected = quote_sheet(mapping[token_a.names[0].casefold()]) + "!" if renamed else raw_a
        if raw_b != expected:
            return False
        cursor_a, cursor_b = token_a.end, token_b.end
    return source[cursor_a:] == candidate[cursor_b:]


def _compare(
    expected: Element,
    actual: Element,
    where: str,
    formula_ok: Callable[[str, str], bool] | None = None,
) -> None:
    if expected.tag != actual.tag:
        raise PreservationError(f"element changed in {where}")
    if expected.attrib != actual.attrib:
        if formula_ok is None or set(expected.attrib) != set(actual.attrib):
            raise PreservationError(f"attributes changed in {where}")
        for key, value in expected.attrib.items():
            other = actual.attrib[key]
            if value != other and not (local_name(key) == "location" and formula_ok(value, other)):
                raise PreservationError(f"attributes changed in {where}")
    if _norm(expected.text) != _norm(actual.text):
        if not (
            formula_ok is not None
            and local_name(expected.tag) in FORMULA_ELEMENTS
            and formula_ok(_norm(expected.text), _norm(actual.text))
        ):
            raise PreservationError(f"text changed in {where}")
    if _norm(expected.tail) != _norm(actual.tail):
        raise PreservationError(f"text changed in {where}")
    if len(expected) != len(actual):
        raise PreservationError(f"child elements changed in {where}")
    for left, right in zip(expected, actual, strict=True):
        _compare(left, right, where, formula_ok)


def _declarations(data: bytes) -> list[bytes]:
    return sorted(match.group(0) for match in _DECLARATIONS.finditer(data))


def verify_preservation(
    source: bytes, candidate: bytes, inventory: WorkbookInventory, edits: list[PlannedEdit]
) -> None:
    try:
        _verify(source, candidate, inventory, edits)
    except PreservationError:
        raise
    except Exception as exc:  # noqa: BLE001 - any unreadable comparison is a failure
        raise PreservationError("the candidate could not be compared safely") from exc


def _verify(
    source: bytes, candidate: bytes, inventory: WorkbookInventory, edits: list[PlannedEdit]
) -> None:
    main, rel_ns = family(inventory)
    with (
        zipfile.ZipFile(io.BytesIO(source)) as before,
        zipfile.ZipFile(io.BytesIO(candidate)) as after,
    ):
        source_infos = {info.filename: info for info in before.infolist()}
        candidate_infos = {info.filename: info for info in after.infolist()}
        if [name for name in candidate_infos if name in source_infos] != list(source_infos):
            raise PreservationError("source entries were removed or reordered")
        tables = plan_new_tables(
            inventory, [edit.correction for edit in edits if edit.correction.kind == "header_new"]
        )
        expected_added = {table.part for table in tables}
        for table in tables:
            rels = _rels_part(table.sheet_part)
            if inventory.package.find(rels) is None:
                expected_added.add(rels)
        if set(candidate_infos) - set(source_infos) != expected_added:
            raise PreservationError("unexpected package entries were added")
        allowed = _allowed_parts(inventory, edits, tables)
        changed: dict[str, tuple[bytes, bytes]] = {}
        for name, info in source_infos.items():
            if candidate_infos[name].compress_type != info.compress_type:
                raise PreservationError(f"compression changed for {name}")
            original, updated = before.read(name), after.read(name)
            if original == updated:
                continue
            if name not in allowed:
                raise PreservationError(f"unapproved change in {name}")
            _check_declarations(name, original, updated, tables, inventory, rel_ns)
            if original.split(b">", 1)[0] != updated.split(b">", 1)[0] and original.startswith(
                (b"<?xml", b"\xef\xbb\xbf<?xml")
            ):
                raise PreservationError(f"XML declaration changed in {name}")
            changed[name] = (original, updated)
        for name, (original, updated) in changed.items():
            _verify_part(name, original, updated, inventory, edits, tables, main, rel_ns)
        for table in tables:
            _verify_new_table(after.read(table.part), table, main)
            rels = _rels_part(table.sheet_part)
            if rels in expected_added:
                _verify_new_rels(
                    after.read(rels),
                    [t for t in tables if t.sheet_part == table.sheet_part],
                    rel_ns,
                )


def _check_declarations(
    name: str,
    original: bytes,
    updated: bytes,
    tables: list[NewTable],
    inventory: WorkbookInventory,
    rel_ns: str,
) -> None:
    """Namespace declarations are retained; only a local r prefix for a new table is added."""
    before, after = _declarations(original), _declarations(updated)
    for declaration in before:
        if declaration not in after:
            raise PreservationError(f"namespace declarations changed in {name}")
        after.remove(declaration)
    allowed = f'xmlns:r="{rel_ns}"'.encode()
    local = [
        table
        for table in tables
        if (inventory.package.find(table.sheet_part) or table.sheet_part) == name
    ]
    if any(item != allowed for item in after) or len(after) > len(local):
        raise PreservationError(f"namespace declarations changed in {name}")


def _rels_part(part: str) -> str:
    folder, _, name = part.rpartition("/")
    return f"{folder}/_rels/{name}.rels"


def _allowed_parts(
    inventory: WorkbookInventory, edits: list[PlannedEdit], tables: list[NewTable]
) -> set[str]:
    allowed: set[str] = set()
    for edit in edits:
        correction = edit.correction
        if correction.kind == "rename":
            allowed.add(inventory.structure.workbook_part)
            allowed.update(
                model.info.part for model in inventory.sheets if model.worksheet is not None
            )
            allowed.update(inventory.chart_formulas)
        else:
            allowed.add(correction.part)
    for table in tables:
        allowed.add(table.sheet_part)
        allowed.add(_rels_part(table.sheet_part))
        allowed.add(CONTENT_TYPES_PART)
    return {inventory.package.find(part) or part for part in allowed}


def _verify_part(
    name: str,
    original: bytes,
    updated: bytes,
    inventory: WorkbookInventory,
    edits: list[PlannedEdit],
    tables: list[NewTable],
    main: str,
    rel_ns: str,
) -> None:
    expected = _parse(original)
    actual = _parse(updated)
    mapping = {
        (edit.correction.sheet_name or "").casefold(): str(edit.value["name"])
        for edit in edits
        if edit.correction.kind == "rename"
    }
    formula_ok = (lambda a, b: rename_equivalent(a, b, mapping)) if mapping else None
    for edit in edits:
        correction = edit.correction
        part = inventory.package.find(correction.part) or correction.part
        if correction.kind == "alt" and part == name:
            matches = [
                node
                for node in expected.iter()
                if local_name(node.tag) == "cNvPr" and node.get("id") == correction.object_id
            ]
            if len(matches) != 1:
                raise PreservationError("alternative-text target is ambiguous")
            matches[0].set("title", str(edit.value["title"]))
            matches[0].set("descr", str(edit.value["description"]))
        elif correction.kind == "link" and part == name:
            assert correction.cell_path is not None and correction.link_path is not None
            cell = find_path(expected, correction.cell_path)
            ns = namespace(cell.tag)
            prefix = f"{{{ns}}}" if ns else ""
            for child in list(cell):
                cell.remove(child)
            cell.text = None
            cell.set("t", "inlineStr")
            inline = SubElement(cell, f"{prefix}is")
            text = SubElement(inline, f"{prefix}t", {f"{{{XML_NS}}}space": "preserve"})
            text.text = str(edit.value["text"])
            link = find_path(expected, correction.link_path)
            if "display" in link.attrib:
                link.set("display", str(edit.value["text"]))
        elif correction.kind == "header_enable" and part == name:
            expected.set("headerRowCount", "1")
        elif correction.kind == "rename" and name == (
            inventory.package.find(inventory.structure.workbook_part)
        ):
            sheet = find_path(
                expected,
                (
                    (local_name(expected.tag), 1),
                    ("sheets", 1),
                    ("sheet", correction.sheet_position or 0),
                ),
            )
            if sheet.get("name") != correction.sheet_name:
                raise PreservationError("renamed worksheet entry moved")
            sheet.set("name", str(edit.value["name"]))
    sheet_tables = [
        table
        for table in tables
        if (inventory.package.find(table.sheet_part) or table.sheet_part) == name
    ]
    if sheet_tables:
        _strip_new_table_parts(actual, expected, sheet_tables, rel_ns)
    rels_tables = [
        t for t in tables if (inventory.package.find(_rels_part(t.sheet_part)) or "") == name
    ]
    if rels_tables:
        _strip_new_relationships(actual, expected, rels_tables, rel_ns)
    if name == CONTENT_TYPES_PART and tables:
        needs_default = any(
            inventory.package.find(_rels_part(t.sheet_part)) is None
            and inventory.package.content_type(_rels_part(t.sheet_part)) is None
            for t in tables
        )
        _strip_new_content_types(actual, expected, tables, needs_default)
    _compare(expected, actual, name, formula_ok)


def _strip_new_table_parts(
    actual: Element, expected: Element, tables: list[NewTable], rel_ns: str
) -> None:
    rel_ids = {table.rel_id for table in tables}
    container = next((child for child in actual if local_name(child.tag) == "tableParts"), None)
    if container is None:
        raise PreservationError("table reference was not added")
    added = [
        child
        for child in container
        if local_name(child.tag) == "tablePart" and child.get(f"{{{rel_ns}}}id") in rel_ids
    ]
    if len(added) != len(tables):
        raise PreservationError("table references do not match the approved headers")
    for child in added:
        if child.attrib != {f"{{{rel_ns}}}id": child.get(f"{{{rel_ns}}}id")} or len(child):
            raise PreservationError("table reference has unexpected content")
        container.remove(child)
    source_container = next(
        (child for child in expected if local_name(child.tag) == "tableParts"), None
    )
    if source_container is None:
        if len(container) or container.attrib != {"count": str(len(tables))}:
            raise PreservationError("new table reference list has unexpected content")
        actual.remove(container)
    else:
        source_container.set(
            "count",
            str(
                len([c for c in source_container if local_name(c.tag) == "tablePart"]) + len(tables)
            ),
        )


def _strip_appended(actual: Element, expected_count: int, what: str) -> list[Element]:
    """The editor appends new children at the end of a container; return and remove them."""
    extra = list(actual)[expected_count:]
    for child in extra:
        if _norm(child.tail) or len(child) or _norm(child.text):
            raise PreservationError(f"{what} additions have unexpected content")
        actual.remove(child)
    return extra


def _strip_new_relationships(
    actual: Element, expected: Element, tables: list[NewTable], rel_ns: str
) -> None:
    extra = _strip_appended(actual, len(expected), "relationship")
    wanted = [(table.rel_id, f"{rel_ns}/table") for table in tables]
    found = [(child.get("Id"), child.get("Type")) for child in extra]
    if found != wanted or any(child.get("TargetMode") for child in extra):
        raise PreservationError("table relationships are not as approved")


def _strip_new_content_types(
    actual: Element, expected: Element, tables: list[NewTable], needs_default: bool
) -> None:
    extra = _strip_appended(actual, len(expected), "content type")
    wanted: list[tuple[str, str | None, str | None]] = [
        ("Override", f"/{table.part}", TABLE_CONTENT_TYPE) for table in tables
    ]
    if needs_default:
        wanted.append(("Default", None, RELS_CONTENT_TYPE))
    found = [
        (local_name(child.tag), child.get("PartName"), child.get("ContentType")) for child in extra
    ]
    if found != wanted:
        raise PreservationError("content types are not as approved")


def _verify_new_rels(data: bytes, tables: list[NewTable], rel_ns: str) -> None:
    root = _parse(data)
    if local_name(root.tag) != "Relationships" or len(root) != len(tables):
        raise PreservationError("new relationship part has unexpected content")
    for child, table in zip(root, tables, strict=True):
        if child.get("Id") != table.rel_id or child.get("Type") != f"{rel_ns}/table":
            raise PreservationError("new relationship part has unexpected content")


def _verify_new_table(data: bytes, table: NewTable, main: str) -> None:
    root = _parse(data)
    rect = table.correction.rect
    assert rect is not None
    if (
        root.tag != f"{{{main}}}table"
        or root.get("id") != str(table.table_id)
        or root.get("name") != table.name
        or root.get("displayName") != table.name
        or root.get("ref") != rect.canonical()
        or root.get("headerRowCount") != "1"
        or root.get("totalsRowCount") not in (None, "0")
    ):
        raise PreservationError("new table metadata is not as approved")
    children = [local_name(child.tag) for child in root]
    if children != ["tableColumns"]:
        raise PreservationError("new table has unexpected structure")
    names = [column.get("name") for column in root[0]]
    if names != list(table.correction.labels):
        raise PreservationError("new table column names do not match the header row")
