"""Semantic snapshot of an .xlsx built on the engine's safe readers (no openpyxl).

Compares values, formulas, style indices, sheet names, defined names, relationships,
content types, part inventory and drawing-object metadata. Experiment code only.
"""

from typing import Any
from xml.etree.ElementTree import Element

from app.contracts.models import ProcessingLimits
from app.workbooks.drawings import inventory_drawing
from app.workbooks.package import OoxmlPackage, rel_type_is
from app.workbooks.structure import read_workbook
from app.workbooks.xmlsafe import local_name


def _shared_strings(package: OoxmlPackage, workbook_part: str) -> list[str]:
    for rel in package.relationships(workbook_part).values():
        if rel_type_is(rel.rel_type, "sharedStrings") and rel.resolved:
            root = package.parse_xml(rel.resolved)
            return [
                "".join(t.text or "" for t in si.iter() if local_name(t.tag) == "t")
                for si in root
                if local_name(si.tag) == "si"
            ]
    return []


def _cells(sheet_root: Element, strings: list[str]) -> dict[str, dict[str, Any]]:
    cells: dict[str, dict[str, Any]] = {}
    for cell in sheet_root.iter():
        if local_name(cell.tag) != "c":
            continue
        kind = cell.get("t", "n")
        value: str | None = None
        formula: str | None = None
        for child in cell:
            name = local_name(child.tag)
            if name == "v":
                value = child.text
            elif name == "f":
                formula = child.text or ""
            elif name == "is":
                value = "".join(t.text or "" for t in child.iter() if local_name(t.tag) == "t")
        if kind == "s" and value is not None:
            value = strings[int(value)]
        semantic_kind = "string" if kind in ("s", "inlineStr", "str") else kind
        cells[cell.get("r", "?")] = {
            "kind": semantic_kind,
            "value": value,
            "formula": formula,
            "style": cell.get("s", "0"),
        }
    return cells


def snapshot(data: bytes) -> dict[str, Any]:
    package = OoxmlPackage(data, ProcessingLimits())
    structure = read_workbook(package, None)
    workbook_root = package.parse_xml(structure.workbook_part)
    strings = _shared_strings(package, structure.workbook_part)
    sheets: dict[str, Any] = {}
    objects: list[dict[str, Any]] = []
    for sheet in structure.sheets:
        root = package.parse_xml(sheet.part)
        sheets[sheet.name] = {"state": sheet.state, "cells": _cells(root, strings)}
        for rel in package.relationships(sheet.part).values():
            if rel_type_is(rel.rel_type, "drawing") and rel.resolved:
                for obj in inventory_drawing(package, rel.resolved).objects:
                    objects.append(
                        {
                            "worksheet": sheet.name,
                            "kind": obj.kind,
                            "id": obj.object_id,
                            "name": obj.name,
                            "title": obj.title,
                            "descr": obj.descr,
                            "decorative": obj.decorative,
                        }
                    )
    defined = {
        (node.get("name", ""), node.get("localSheetId")): node.text or ""
        for node in workbook_root.iter()
        if local_name(node.tag) == "definedName"
    }
    relationships = sorted(
        (name, rel.rel_id, rel.rel_type.rpartition("/")[2], rel.resolved or rel.target)
        for name in package.entry_names
        if name.casefold().endswith(".rels")
        for rel in package.parse_rels(name).values()
    )
    return {
        "sheet_order": [sheet.name for sheet in structure.sheets],
        "sheets": sheets,
        "defined_names": {f"{k[0]}@{k[1]}": v for k, v in sorted(defined.items(), key=str)},
        "objects": sorted(objects, key=lambda o: (o["worksheet"], str(o["id"]), o["name"])),
        "relationship_types": sorted({r[2] for r in relationships}),
        "relationship_count": len(relationships),
        "parts": package.entry_names,
    }


def diff(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Human-readable semantic differences, in a stable order."""
    out: list[str] = []
    if before["sheet_order"] != after["sheet_order"]:
        out.append(f"sheet names {before['sheet_order']} -> {after['sheet_order']}")
    for name, sheet in before["sheets"].items():
        other = after["sheets"].get(name)
        if other is None:
            continue
        for ref, cell in sheet["cells"].items():
            new = other["cells"].get(ref)
            if new is None:
                out.append(f"{name}!{ref} removed")
                continue
            for key in ("kind", "value", "formula", "style"):
                if cell[key] != new[key]:
                    out.append(f"{name}!{ref} {key} {cell[key]!r} -> {new[key]!r}")
        for ref in sorted(set(other["cells"]) - set(sheet["cells"])):
            out.append(f"{name}!{ref} added")
    if before["defined_names"] != after["defined_names"]:
        out.append(f"defined names {before['defined_names']} -> {after['defined_names']}")
    if before["objects"] != after["objects"]:
        lost = len(before["objects"]) - len(after["objects"])
        out.append(f"drawing objects changed (count delta {-lost:+d})")
        after_by_key = {(o["worksheet"], o["id"]): o for o in after["objects"]}
        for obj in before["objects"]:
            match = after_by_key.get((obj["worksheet"], obj["id"]))
            if match is None:
                out.append(f"  object {obj['kind']} id={obj['id']} {obj['name']!r} missing")
            elif match != obj:
                changed = sorted(k for k in obj if obj[k] != match[k])
                out.append(f"  object id={obj['id']} fields changed: {changed}")
        before_keys = {(o["worksheet"], o["id"]) for o in before["objects"]}
        for obj in after["objects"]:
            if (obj["worksheet"], obj["id"]) not in before_keys:
                out.append(f"  object {obj['kind']} id={obj['id']} {obj['name']!r} only after")
    if before["relationship_types"] != after["relationship_types"]:
        out.append(
            f"relationship types {before['relationship_types']} -> {after['relationship_types']}"
        )
    removed = sorted(set(before["parts"]) - set(after["parts"]))
    added = sorted(set(after["parts"]) - set(before["parts"]))
    if removed:
        out.append(f"parts removed {removed}")
    if added:
        out.append(f"parts added {added}")
    return out
