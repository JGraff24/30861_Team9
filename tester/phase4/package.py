"""The tester's own OOXML package comparator for Phase 4 outputs (§11, P4-PRESERVATION).

It never calls the system's editor or preservation comparator. Rules:
- Every entry not touched by an approved effect must have byte-identical decompressed content.
- Entry sets must match, except for parts an approved header-range effect adds.
- A changed XML part is parsed safely; each approved effect first proves its own positive
  result, then masks exactly the attributes/elements it is allowed to change in both trees.
  Whatever remains must compare equal element by element (namespaces by URI, attributes,
  text, comments). Whitespace-only text between elements is not significant; leaf text is.
"""

import io
import posixpath
import zipfile
import zlib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from xml.etree.ElementTree import Element, TreeBuilder

import defusedxml.ElementTree as safe_xml  # type: ignore[import-untyped]
from defusedxml import DefusedXmlException

MAX_ENTRIES = 10_000
MAX_EXPANDED_BYTES = 209_715_200
MAX_PART_BYTES = 52_428_800
TABLE_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"
TABLE_REL_TYPES = {
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/table",
    "http://purl.oclc.org/ooxml/officeDocument/relationships/table",
}
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
MC_ATTRIBUTES = {
    f"{{{MC}}}{name}"
    for name in (
        "Ignorable",
        "ProcessContent",
        "MustUnderstand",
        "PreserveElements",
        "PreserveAttributes",
    )
}
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
HEADER_METADATA = ("headerRowDxfId", "headerRowBorderDxfId", "headerRowCellStyle")
R_NAMESPACES = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
)


class PackageError(ValueError):
    """The bytes are not a safe, readable OOXML ZIP package."""


def read_package(data: bytes) -> dict[str, bytes]:
    """Decompressed entries by name, enforcing the §6 ZIP ceilings and rejecting duplicates."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        infos = archive.infolist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise PackageError("not a readable ZIP package") from exc
    if len(infos) > MAX_ENTRIES:
        raise PackageError("too many ZIP entries")
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise PackageError("duplicate ZIP entry names")
    entries: dict[str, bytes] = {}
    total = 0
    for info in infos:
        normalized = posixpath.normpath(info.filename)
        if info.filename.startswith("/") or normalized.startswith("..") or "\\" in info.filename:
            raise PackageError("unsafe ZIP entry path")
        if info.flag_bits & 0x1:
            raise PackageError("encrypted ZIP entry")
        try:
            with archive.open(info) as stream:
                content = stream.read(MAX_PART_BYTES + 1)
        except (
            zipfile.BadZipFile,
            zlib.error,
            NotImplementedError,
            EOFError,
            RuntimeError,
            ValueError,
            OSError,
        ) as exc:
            raise PackageError("a ZIP entry could not be decompressed") from exc
        if len(content) > MAX_PART_BYTES:
            raise PackageError("ZIP entry exceeds the per-part ceiling")
        total += len(content)
        if total > MAX_EXPANDED_BYTES:
            raise PackageError("expanded package exceeds the total ceiling")
        entries[info.filename] = content
    return entries


def parse_xml(content: bytes) -> Element:
    """Parse with DTDs/entities/external references forbidden, keeping comments and PIs."""
    try:
        parser = safe_xml.DefusedXMLParser(
            target=TreeBuilder(insert_comments=True, insert_pis=True), forbid_dtd=True
        )
        parser.feed(content)
        root: Element = parser.close()
    except (DefusedXmlException, SyntaxError, ValueError) as exc:
        raise PackageError("unsafe or malformed XML part") from exc
    return root


def declarations(content: bytes) -> list[tuple[str, str]]:
    """Every (prefix, namespace URI) declared anywhere in a part, in document order."""
    try:
        events = safe_xml.iterparse(io.BytesIO(content), events=("start-ns",), forbid_dtd=True)
        return [(prefix or "", uri) for _, (prefix, uri) in events]
    except (DefusedXmlException, SyntaxError, ValueError) as exc:
        raise PackageError("unsafe or malformed XML part") from exc


def scopes(content: bytes) -> list[dict[str, str]]:
    """The in-scope prefix map of every element, in document order (comments excluded)."""
    found: list[dict[str, str]] = []
    stack: list[dict[str, str]] = [{"xml": "http://www.w3.org/XML/1998/namespace"}]
    declared: dict[str, str] = {}
    try:
        for event, item in safe_xml.iterparse(
            io.BytesIO(content), events=("start-ns", "start", "end"), forbid_dtd=True
        ):
            if event == "start-ns":
                prefix, uri = item
                declared[prefix or ""] = uri
            elif event == "start":
                scope = {**stack[-1], **declared}
                declared = {}
                stack.append(scope)
                found.append(scope)
            else:
                stack.pop()
    except (DefusedXmlException, SyntaxError, ValueError) as exc:
        raise PackageError("unsafe or malformed XML part") from exc
    return found


def normalize_compatibility(root: Element, element_scopes: list[dict[str, str]]) -> list[str]:
    """Rewrite mc:* prefix lists to sorted namespace URIs; report prefixes not in scope.

    Markup-compatibility attributes name namespaces by prefix, which ElementTree does not
    resolve. Each element's prefixes are resolved against the declarations in scope at that
    element, so a legitimate re-prefixing compares equal while a dropped, moved-out-of-scope
    or undeclared namespace is caught (section 11).
    """
    undeclared = []
    elements = [e for e in root.iter() if isinstance(e.tag, str)]
    for element, prefixes in zip(elements, element_scopes, strict=False):
        for name in MC_ATTRIBUTES & set(element.attrib):
            tokens = []
            for token in element.attrib[name].split():
                prefix, _, rest = token.partition(":")
                uri = prefixes.get(prefix)
                if uri is None:
                    undeclared.append(prefix)
                    tokens.append(f"?{prefix}:{rest}")
                else:
                    tokens.append(f"{{{uri}}}{rest}")
            element.attrib[name] = " ".join(sorted(tokens))
    return undeclared


def local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def descendants(root: Element, name: str) -> Iterator[Element]:
    for element in root.iter():
        if local(element.tag) == name:
            yield element


def r_attribute(element: Element, name: str) -> str | None:
    for namespace in R_NAMESPACES:
        value = element.get(f"{{{namespace}}}{name}")
        if value is not None:
            return value
    return None


def text_of(element: Element | None) -> str:
    """Concatenate text runs (`t` descendants) in document order, without phonetic runs."""
    if element is None:
        return ""
    parts = []
    for node in element.iter():
        if local(node.tag) == "rPh":
            continue
        if local(node.tag) == "t" and not _inside_phonetic(element, node):
            parts.append(node.text or "")
    return "".join(parts)


def _inside_phonetic(root: Element, target: Element) -> bool:
    for phonetic in descendants(root, "rPh"):
        if any(node is target for node in phonetic.iter()):
            return True
    return False


# --- semantic tree comparison -------------------------------------------------------------


def _significant(text: str | None, leaf: bool) -> str:
    value = text or ""
    if not leaf and not value.strip():
        return ""
    return value


def differences(source: Element, output: Element, path: str = "") -> list[str]:
    """Element-by-element differences as short location descriptions (no content values)."""
    here = f"{path}/{local(source.tag) or 'comment'}"
    if source.tag != output.tag:
        return [f"{here}: element changed"]
    found = []
    before, after = dict(source.attrib), dict(output.attrib)
    if local(source.tag) == "c":
        # SpreadsheetML's default cell style index is 0, so s="0" equals an omitted s.
        for attributes in (before, after):
            if attributes.get("s") == "0":
                del attributes["s"]
    if all((e.text or "") == (e.text or "").strip() for e in (source, output)):
        # xml:space="preserve" is meaningless when the text has no edge whitespace.
        before.pop(XML_SPACE, None)
        after.pop(XML_SPACE, None)
    if before != after:
        changed = sorted(
            local(key) for key in set(before) | set(after) if before.get(key) != after.get(key)
        )
        found.append(f"{here}: attribute(s) {', '.join(changed)} changed")
    leaf = len(source) == 0 and len(output) == 0
    if _significant(source.text, leaf) != _significant(output.text, leaf):
        found.append(f"{here}: text changed")
    if len(source) != len(output):
        found.append(f"{here}: child count {len(source)} -> {len(output)}")
        return found
    counters: dict[str, int] = {}
    for left, right in zip(source, output, strict=True):
        name = local(left.tag) or "comment"
        counters[name] = counters.get(name, 0) + 1
        found.extend(differences(left, right, f"{here}[{counters[name]}]" if path else here))
        if _significant(left.tail, False) != _significant(right.tail, False):
            found.append(f"{here}/{name}: trailing text changed")
    return found


# --- approved effects ---------------------------------------------------------------------


@dataclass
class PartRule:
    """For one changed part: a positive check on the output, and masks for both trees."""

    verify: list[Callable[[Element, Element], list[str]]] = field(default_factory=list)
    mask: list[Callable[[Element, Element], None]] = field(default_factory=list)


@dataclass
class EffectPlan:
    rules: dict[str, PartRule] = field(default_factory=dict)
    added: dict[str, Callable[[bytes], list[str]]] = field(default_factory=dict)

    def rule(self, part: str) -> PartRule:
        return self.rules.setdefault(part, PartRule())


def _cnvpr(root: Element, object_id: str) -> list[Element]:
    return [e for e in descendants(root, "cNvPr") if e.get("id") == object_id]


def _alt(plan: EffectPlan, effect: Mapping[str, Any]) -> None:
    object_id, title, descr = str(effect["id"]), effect["title"], effect["descr"]

    def verify(source: Element, output: Element) -> list[str]:
        matches = _cnvpr(output, object_id)
        if len(matches) != 1:
            return [f"alt: object id {object_id} is not unique in the output"]
        if matches[0].get("title") != title or matches[0].get("descr") != descr:
            return [f"alt: object id {object_id} title/description differ from the approved pair"]
        return []

    def mask(source: Element, output: Element) -> None:
        for tree in (source, output):
            for element in _cnvpr(tree, object_id):
                element.attrib.pop("title", None)
                element.attrib.pop("descr", None)

    rule = plan.rule(effect["part"])
    rule.verify.append(verify)
    rule.mask.append(mask)


def _cells(root: Element, ref: str) -> list[Element]:
    return [e for e in descendants(root, "c") if e.get("r") == ref]


def _hyperlinks(root: Element, ref: str) -> list[Element]:
    return [e for e in descendants(root, "hyperlink") if e.get("ref") == ref]


def _link(plan: EffectPlan, effect: Mapping[str, Any]) -> None:
    ref, label = effect["cell"], effect["text"]

    def verify(source: Element, output: Element) -> list[str]:
        cells, original = _cells(output, ref), _cells(source, ref)
        if len(cells) != 1 or len(original) != 1:
            return [f"link: cell {ref} is missing or repeated"]
        cell = cells[0]
        problems = []
        if cell.get("t") != "inlineStr" or any(local(c.tag) == "f" for c in cell):
            problems.append(f"link: {ref} is not an explicit inline-string text cell")
        inline = [c for c in cell if local(c.tag) == "is"]
        if len(inline) != 1 or text_of(inline[0]) != label:
            problems.append(f"link: {ref} text differs from the approved label")
        if cell.get("s", "0") != original[0].get("s", "0"):
            problems.append(f"link: {ref} style index changed")
        links = _hyperlinks(output, ref)
        if len(links) != 1:
            problems.append(f"link: {ref} hyperlink declaration missing or repeated")
        elif links[0].get("display") not in (None, label):
            problems.append(f"link: {ref} display attribute differs from the approved label")
        return problems

    def mask(source: Element, output: Element) -> None:
        for tree in (source, output):
            for cell in _cells(tree, ref):
                keep = {"r": cell.get("r", "")}
                if cell.get("s", "0") != "0":
                    keep["s"] = cell.get("s", "0")
                cell.attrib.clear()
                cell.attrib.update(keep)
                for child in list(cell):
                    cell.remove(child)
                cell.text = None
            for link in _hyperlinks(tree, ref):
                link.attrib.pop("display", None)

    rule = plan.rule(effect["part"])
    rule.verify.append(verify)
    rule.mask.append(mask)


def _reference_elements(root: Element, reference: Mapping[str, Any]) -> list[Element]:
    kind = reference["kind"]
    if kind == "cell_formula":
        return [f for c in _cells(root, reference["ref"]) for f in c if local(f.tag) == "f"]
    if kind == "defined_name":
        return [e for e in descendants(root, "definedName") if e.get("name") == reference["name"]]
    if kind == "chart_formula":
        formulas = [e for e in descendants(root, "f")]
        index = int(reference["index"])
        return [formulas[index]] if index < len(formulas) else []
    if kind == "validation":
        return [
            f
            for v in descendants(root, "dataValidation")
            if v.get("sqref") == reference["sqref"]
            for f in v
            if local(f.tag) == "formula1"
        ]
    if kind == "conditional":
        # Main-namespace rules carry @sqref and <formula>; x14 extension rules carry an
        # <xm:sqref> child and <xm:f>.
        found: list[Element] = []
        for block in descendants(root, "conditionalFormatting"):
            sqref = block.get("sqref") or next(
                (e.text for e in block if local(e.tag) == "sqref"), None
            )
            if sqref == reference["sqref"]:
                found.extend(f for f in block.iter() if local(f.tag) in ("formula", "f"))
        return found[:1]
    if kind == "hyperlink_location":
        return _hyperlinks(root, reference["ref"])
    raise ValueError(f"unknown reference kind {kind}")


def _rename(plan: EffectPlan, effect: Mapping[str, Any]) -> None:
    old, new = effect["sheet"], effect["name"]

    def sheet(root: Element, name: str) -> list[Element]:
        return [e for e in descendants(root, "sheet") if e.get("name") == name]

    def verify_workbook(source: Element, output: Element) -> list[str]:
        before = sheet(source, old)
        after = [
            e
            for e in descendants(output, "sheet")
            if before and e.get("sheetId") == before[0].get("sheetId")
        ]
        if len(before) != 1 or len(after) != 1 or after[0].get("name") != new:
            return [f"rename: workbook sheet {old!r} was not renamed to the approved name"]
        return []

    def mask_workbook(source: Element, output: Element) -> None:
        for element in sheet(source, old):
            element.attrib.pop("name")
        for element in sheet(output, new):
            element.attrib.pop("name")

    def mask_titles(source: Element, output: Element) -> None:
        # Extended properties may list sheet titles (TitlesOfParts); only this name may change.
        for element in descendants(source, "lpstr"):
            if element.text == old:
                element.text = None
        for element in descendants(output, "lpstr"):
            if element.text == new:
                element.text = None

    plan.rule("docProps/app.xml").mask.append(mask_titles)

    workbook = plan.rule("xl/workbook.xml")
    workbook.verify.append(verify_workbook)
    workbook.mask.append(mask_workbook)

    for reference in effect["references"]:
        attribute = "location" if reference["kind"] == "hyperlink_location" else None
        expected = reference.get("location") if attribute else reference["formula"]

        def verify(
            source: Element,
            output: Element,
            reference: Mapping[str, Any] = reference,
            attribute: str | None = attribute,
            expected: str = expected,
        ) -> list[str]:
            found = _reference_elements(output, reference)
            if len(found) != 1:
                return [f"rename: reference {reference['kind']} in {reference['part']} not found"]
            value = found[0].get(attribute) if attribute else found[0].text
            if value != expected:
                return [
                    f"rename: {reference['kind']} in {reference['part']} was not rewritten "
                    "to the approved quoted name"
                ]
            return []

        def mask(
            source: Element,
            output: Element,
            reference: Mapping[str, Any] = reference,
            attribute: str | None = attribute,
        ) -> None:
            for tree in (source, output):
                for element in _reference_elements(tree, reference):
                    if attribute:
                        element.attrib.pop(attribute, None)
                    else:
                        element.text = None

        rule = plan.rule(reference["part"])
        rule.verify.append(verify)
        rule.mask.append(mask)


def _resolve(base_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(
        posixpath.join(posixpath.dirname(posixpath.dirname(base_part)), target)
    )


def _header_range(plan: EffectPlan, effect: Mapping[str, Any], source_parts: set[str]) -> None:
    sheet_part, rel_id = effect["sheet_part"], effect["rel_id"]
    table_part, rels_part = effect["table_part"], effect["sheet_rels_part"]

    def table_parts(root: Element) -> list[Element]:
        return [e for e in descendants(root, "tablePart") if r_attribute(e, "id") == rel_id]

    def verify_sheet(source: Element, output: Element) -> list[str]:
        if len(table_parts(output)) != 1:
            return ["header: sheet lacks the new tablePart"]
        containers = [e for e in output if local(e.tag) == "tableParts"]
        if len(containers) != 1 or len(list(descendants(output, "tableParts"))) != 1:
            return ["header: the worksheet needs exactly one tableParts child of the root"]
        children = list(output)
        after = children[children.index(containers[0]) + 1 :]
        if any(local(e.tag) != "extLst" for e in after):
            return ["header: tableParts is not the last worksheet child before extLst"]
        if containers[0].get("count") != str(len(containers[0])):
            return ["header: tableParts count does not equal its tablePart children"]
        return []

    def mask_sheet(source: Element, output: Element) -> None:
        for container in list(descendants(output, "tableParts")):
            for element in [e for e in container if r_attribute(e, "id") == rel_id]:
                container.remove(element)
            if len(container) == 0 and not list(descendants(source, "tableParts")):
                for parent in output.iter():
                    if container in list(parent):
                        parent.remove(container)
        for tree in (source, output):
            for container in descendants(tree, "tableParts"):
                container.attrib.pop("count", None)

    rule = plan.rule(sheet_part)
    rule.verify.append(verify_sheet)
    rule.mask.append(mask_sheet)

    def relationship_problems(root: Element) -> list[str]:
        matches = [e for e in root if e.get("Id") == rel_id]
        if len(matches) != 1 or matches[0].get("Type") not in TABLE_REL_TYPES:
            return ["header: sheet relationship to the new table is missing"]
        if _resolve(rels_part, matches[0].get("Target", "")) != table_part:
            return ["header: sheet relationship does not target the new table part"]
        if matches[0].get("TargetMode") not in (None, "Internal"):
            return ["header: table relationship must be internal"]
        return []

    if rels_part in source_parts:
        rels = plan.rule(rels_part)
        rels.verify.append(lambda source, output: relationship_problems(output))

        def mask_rels(source: Element, output: Element) -> None:
            for element in [e for e in output if e.get("Id") == rel_id]:
                output.remove(element)

        rels.mask.append(mask_rels)
    else:

        def check_new_rels(content: bytes) -> list[str]:
            root = parse_xml(content)
            extra = [e for e in root if e.get("Id") != rel_id]
            return relationship_problems(root) + (
                ["header: new sheet relationships part holds unrelated entries"] if extra else []
            )

        plan.added[rels_part] = check_new_rels

    def check_table(content: bytes) -> list[str]:
        root = parse_xml(content)
        problems = []
        if local(root.tag) != "table":
            return ["header: new table part has no table root"]
        if root.get("id") != str(effect["table_id"]):
            problems.append("header: table id is not the lowest unused positive id")
        if root.get("name") != effect["name"] or root.get("displayName") != effect["name"]:
            problems.append(
                "header: table name/displayName is not the deterministic A11yTable name"
            )
        if root.get("ref") != effect["range"]:
            problems.append("header: table ref is not the exact reported rectangle")
        if root.get("headerRowCount", "1") != "1":
            problems.append("header: headerRowCount is not 1")
        if root.get("totalsRowCount", "0") != "0":
            problems.append("header: new table declares a totals row")
        if list(descendants(root, "autoFilter")):
            problems.append("header: new table declares a filter")
        if any(e.get("name") for e in descendants(root, "tableStyleInfo")):
            problems.append("header: new table applies a style overlay")
        columns = [e.get("name") for e in descendants(root, "tableColumn")]
        for container in descendants(root, "tableColumns"):
            if container.get("count") != str(len(columns)):
                problems.append("header: tableColumns count does not equal its columns")
        if columns != list(effect["columns"]):
            problems.append("header: table column names differ from the confirmed header labels")
        ids = [e.get("id") for e in descendants(root, "tableColumn")]
        if len(set(ids)) != len(ids):
            problems.append("header: table column ids repeat")
        return problems

    plan.added[table_part] = check_table

    def overrides(root: Element) -> list[Element]:
        return [e for e in root if e.get("PartName") == "/" + table_part]

    types = plan.rule("[Content_Types].xml")
    types.verify.append(
        lambda source, output: (
            []
            if len(overrides(output)) == 1
            and overrides(output)[0].get("ContentType") == TABLE_CONTENT_TYPE
            else ["header: content type override for the new table is missing"]
        )
    )

    def mask_types(source: Element, output: Element) -> None:
        for element in overrides(output):
            output.remove(element)

    types.mask.append(mask_types)


def _header_table(plan: EffectPlan, effect: Mapping[str, Any]) -> None:
    def verify(source: Element, output: Element) -> list[str]:
        if local(output.tag) != "table" or output.get("headerRowCount", "1") != "1":
            return ["header: formal table header was not enabled"]
        return []

    def mask(source: Element, output: Element) -> None:
        # Section 8.6 allows headerRowCount plus "associated header metadata if necessary".
        for tree in (source, output):
            for name in ("headerRowCount", *HEADER_METADATA):
                tree.attrib.pop(name, None)
        if not [e for e in source if local(e.tag) == "autoFilter"]:
            for element in [e for e in output if local(e.tag) == "autoFilter"]:
                if element.get("ref") == output.get("ref"):
                    output.remove(element)

    rule = plan.rule(effect["table_part"])
    rule.verify.append(verify)
    rule.mask.append(mask)


def plan_effects(effects: list[Mapping[str, Any]], source_parts: set[str]) -> EffectPlan:
    plan = EffectPlan()
    for effect in effects:
        kind = effect["type"]
        if kind == "alt":
            _alt(plan, effect)
        elif kind == "link":
            _link(plan, effect)
        elif kind == "rename":
            _rename(plan, effect)
        elif kind == "header_range":
            _header_range(plan, effect, source_parts)
        elif kind == "header_table":
            _header_table(plan, effect)
        else:
            raise ValueError(f"unknown effect type {kind}")
    return plan


def compare_packages(
    source_bytes: bytes, output_bytes: bytes, effects: list[Mapping[str, Any]]
) -> list[str]:
    """Problems found; an empty list means exactly the approved differences were observed."""
    source, output = read_package(source_bytes), read_package(output_bytes)
    # ZIP directory entries ("xl/") carry no part content.
    source = {name: data for name, data in source.items() if not name.endswith("/")}
    output = {name: data for name, data in output.items() if not name.endswith("/")}
    plan = plan_effects(effects, set(source))
    problems = []
    missing = sorted(set(source) - set(output))
    added = sorted(set(output) - set(source))
    if missing:
        problems.append(f"output is missing {len(missing)} source entr(y/ies), first {missing[0]}")
    unexpected = [name for name in added if name not in plan.added]
    if unexpected:
        problems.append(f"output adds unexplained entry {unexpected[0]}")
    for name, check in plan.added.items():
        if name not in output:
            problems.append(f"approved addition {name} is absent")
        else:
            problems.extend(check(output[name]))
    for name in sorted(set(source) & set(output)):
        if source[name] == output[name]:
            continue
        rule = plan.rules.get(name)
        if rule is None:
            problems.append(f"unexplained change in {name}")
            continue
        try:
            before, after = parse_xml(source[name]), parse_xml(output[name])
            source_ns, output_ns = declarations(source[name]), declarations(output[name])
        except PackageError:
            problems.append(f"changed part {name} is not safe, well-formed XML")
            continue
        dropped = {uri for _, uri in source_ns} - {uri for _, uri in output_ns}
        if dropped:
            problems.append(
                f"changed part {name} no longer declares namespace {sorted(dropped)[0]}"
            )
        normalize_compatibility(before, scopes(source[name]))
        if normalize_compatibility(after, scopes(output[name])):
            problems.append(f"changed part {name} uses an undeclared prefix in mc: attributes")
        for verify in rule.verify:
            problems.extend(verify(before, after))
        for mask in rule.mask:
            mask(before, after)
        try:
            remaining = differences(before, after)
        except RecursionError:
            problems.append(f"changed part {name} is nested too deeply to compare")
            continue
        if remaining:
            problems.append(f"unexplained semantic change in {name}: {remaining[0]}")
    for name, rule in plan.rules.items():
        if name in source and name in output and source[name] == output[name] and rule.verify:
            problems.append(f"approved change to {name} did not take effect")
    return problems
