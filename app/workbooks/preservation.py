"""Independent preservation comparison (specification section 11).

The editor works on raw text spans; this verifier re-reads both packages with the
hardened tree parser and rebuilds the expected result from the source tree and the
explicit approved-change allowlist. Every untouched entry must have byte-identical
uncompressed contents; every changed XML part must equal its expected tree exactly.
"""

import io
import posixpath
import re
import zipfile
from collections.abc import Callable
from xml.etree.ElementTree import Element, SubElement, TreeBuilder

import defusedxml.ElementTree as safe_xml  # type: ignore[import-untyped]

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
from app.workbooks.xmlsafe import local_name, namespace
from app.workbooks.xmltext import XML_NS

XmlPath = tuple[tuple[str, int], ...]
_DECLARATIONS = re.compile(rb"xmlns(?::[A-Za-z0-9_.\-]+)?\s*=\s*(\"[^\"]*\"|'[^']*')")
FORMULA_ELEMENTS = frozenset({"f", "formula", "formula1", "formula2", "definedName"})
PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


class PreservationError(Exception):
    """The candidate differs from the source beyond the approved changes."""


class _PreservationBuilder(TreeBuilder):
    """Retain document-level comments/PIs as well as nodes inside the root."""

    def __init__(self, outer: list[tuple[bool, str, str]]) -> None:
        super().__init__(insert_comments=True, insert_pis=True)
        self.outer = outer
        self.depth = 0
        self.root_seen = False

    def start(self, tag: str, attrs: dict[str, str]) -> Element:
        self.depth += 1
        self.root_seen = True
        return super().start(tag, attrs)

    def end(self, tag: str) -> Element:
        element = super().end(tag)
        self.depth -= 1
        return element

    def comment(self, text: str | None) -> Element:
        if self.depth == 0:
            self.outer.append((self.root_seen, "comment", text or ""))
        return super().comment(text)

    def pi(self, target: str, text: str | None = None) -> Element:
        if self.depth == 0:
            self.outer.append((self.root_seen, target, text or ""))
        return super().pi(target, text)


def _parse(data: bytes, *, outer: list[tuple[bool, str, str]] | None = None) -> Element:
    # The audit parser may omit comments, but a preservation comparison may not.
    parser = safe_xml.DefusedXMLParser(
        target=_PreservationBuilder(outer if outer is not None else []),
        forbid_dtd=True,
        forbid_entities=True,
        forbid_external=True,
    )
    root: Element = safe_xml.parse(io.BytesIO(data), parser=parser).getroot()
    return root


def _local(element: Element) -> str | None:
    return local_name(element.tag) if isinstance(element.tag, str) else None


def find_path(root: Element, path: XmlPath) -> Element:
    if not path or _local(root) != path[0][0]:
        raise PreservationError("approved target path does not resolve")
    current = root
    for name, index in path[1:]:
        matches = [child for child in current if _local(child) == name]
        if len(matches) < index:
            raise PreservationError("approved target path does not resolve")
        current = matches[index - 1]
    return current


def _norm(text: str | None) -> str:
    return text or ""


def rename_equivalent(source: str, candidate: str, mapping: dict[str, str]) -> bool:
    """Same formula except single-sheet prefixes naming a renamed sheet."""
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
    if set(expected.attrib) != set(actual.attrib):
        raise PreservationError(f"attributes changed in {where}")
    for key, value in expected.attrib.items():
        other = actual.attrib[key]
        if formula_ok is not None and _local(expected) == "hyperlink" and key == "location":
            if not formula_ok(value, other):
                raise PreservationError(f"hyperlink reference changed incorrectly in {where}")
        elif value != other:
            raise PreservationError(f"attributes changed in {where}")
    if formula_ok is not None and _local(expected) in FORMULA_ELEMENTS:
        if not formula_ok(_norm(expected.text), _norm(actual.text)):
            raise PreservationError(f"formula reference changed incorrectly in {where}")
    elif _norm(expected.text) != _norm(actual.text):
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
        if any(edit.correction.kind == "rename" for edit in edits):
            # Unchanged reference-bearing parts also require comparison: leaving
            # an old sheet token behind is a missing approved dependency rewrite.
            for name in allowed:
                if name in source_infos and name not in changed:
                    changed[name] = (before.read(name), after.read(name))
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
    original_outer: list[tuple[bool, str, str]] = []
    updated_outer: list[tuple[bool, str, str]] = []
    expected = _parse(original, outer=original_outer)
    actual = _parse(updated, outer=updated_outer)
    if original_outer != updated_outer:
        raise PreservationError(f"document comments or processing instructions changed in {name}")
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
                if _local(node) == "cNvPr" and node.get("id") == correction.object_id
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
                    (_local(expected) or "", 1),
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
    container = next((child for child in actual if _local(child) == "tableParts"), None)
    if container is None:
        raise PreservationError("table reference was not added")
    added = [
        child
        for child in container
        if _local(child) == "tablePart" and child.get(f"{{{rel_ns}}}id") in rel_ids
    ]
    if len(added) != len(tables):
        raise PreservationError("table references do not match the approved headers")
    for child in added:
        if (
            child.tag != f"{{{namespace(actual.tag)}}}tablePart"
            or child.attrib != {f"{{{rel_ns}}}id": child.get(f"{{{rel_ns}}}id")}
            or len(child)
            or _norm(child.text)
            or _norm(child.tail)
        ):
            raise PreservationError("table reference has unexpected content")
        container.remove(child)
    source_container = next((child for child in expected if _local(child) == "tableParts"), None)
    if source_container is None:
        if (
            container.tag != f"{{{namespace(actual.tag)}}}tableParts"
            or len(container)
            or container.attrib != {"count": str(len(tables))}
            or _norm(container.text)
            or _norm(container.tail)
        ):
            raise PreservationError("new table reference list has unexpected content")
        actual.remove(container)
    else:
        source_container.set(
            "count",
            str(len([c for c in source_container if _local(c) == "tablePart"]) + len(tables)),
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
    wanted = [
        {
            "Id": table.rel_id,
            "Type": f"{rel_ns}/table",
            "Target": posixpath.relpath(table.part, posixpath.dirname(table.sheet_part)),
        }
        for table in tables
    ]
    if [child.attrib for child in extra] != wanted or any(
        child.tag != f"{{{PACKAGE_REL}}}Relationship" for child in extra
    ):
        raise PreservationError("table relationships are not as approved")


def _strip_new_content_types(
    actual: Element, expected: Element, tables: list[NewTable], needs_default: bool
) -> None:
    extra = _strip_appended(actual, len(expected), "content type")
    content_ns = namespace(expected.tag)
    wanted: list[tuple[str, dict[str, str]]] = [
        (
            f"{{{content_ns}}}Override",
            {"PartName": f"/{table.part}", "ContentType": TABLE_CONTENT_TYPE},
        )
        for table in tables
    ]
    if needs_default:
        wanted.append(
            (
                f"{{{content_ns}}}Default",
                {"Extension": "rels", "ContentType": RELS_CONTENT_TYPE},
            )
        )
    found = [(child.tag, child.attrib) for child in extra]
    if found != wanted:
        raise PreservationError("content types are not as approved")


def _verify_new_rels(data: bytes, tables: list[NewTable], rel_ns: str) -> None:
    outer: list[tuple[bool, str, str]] = []
    root = _parse(data, outer=outer)
    if (
        root.tag != f"{{{PACKAGE_REL}}}Relationships"
        or root.attrib
        or len(root) != len(tables)
        or _norm(root.text)
        or _norm(root.tail)
        or outer
    ):
        raise PreservationError("new relationship part has unexpected content")
    for child, table in zip(root, tables, strict=True):
        if (
            child.tag != f"{{{PACKAGE_REL}}}Relationship"
            or child.attrib
            != {
                "Id": table.rel_id,
                "Type": f"{rel_ns}/table",
                "Target": posixpath.relpath(table.part, posixpath.dirname(table.sheet_part)),
            }
            or len(child)
            or _norm(child.text)
            or _norm(child.tail)
        ):
            raise PreservationError("new relationship part has unexpected content")


def _verify_new_table(data: bytes, table: NewTable, main: str) -> None:
    outer: list[tuple[bool, str, str]] = []
    root = _parse(data, outer=outer)
    if outer:
        raise PreservationError("new table has unapproved document markup")
    rect = table.correction.rect
    assert rect is not None
    expected = Element(
        f"{{{main}}}table",
        {
            "id": str(table.table_id),
            "name": table.name,
            "displayName": table.name,
            "ref": rect.canonical(),
            "headerRowCount": "1",
            "totalsRowShown": "0",
        },
    )
    columns = SubElement(
        expected, f"{{{main}}}tableColumns", {"count": str(len(table.correction.labels))}
    )
    for index, label in enumerate(table.correction.labels, start=1):
        SubElement(columns, f"{{{main}}}tableColumn", {"id": str(index), "name": label})
    _compare(expected, root, "new table")
