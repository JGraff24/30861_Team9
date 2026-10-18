"""Drawing object inventory read directly from DrawingML and legacy VML parts.

Library object models omit shapes and alt-text metadata, so the XML is walked directly.
Representations that cannot be inspected become UnsupportedObject records; they are
never dropped.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal
from xml.etree.ElementTree import Element

from app.contracts.errors import WorkbookValidationError
from app.workbooks.package import OoxmlPackage
from app.workbooks.xmlsafe import local_name, namespace

XmlPath = tuple[tuple[str, int], ...]
Decorative = Literal["absent", "true", "false", "ambiguous"]

XDR_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
        "http://purl.oclc.org/ooxml/drawingml/spreadsheetDrawing",
    }
)
MC_NAMESPACE = "http://schemas.openxmlformats.org/markup-compatibility/2006"
DECORATIVE_TAG = "{http://schemas.microsoft.com/office/drawing/2017/decorative}decorative"
VML_NAMESPACE = "urn:schemas-microsoft-com:vml"
VML_EXCEL_NAMESPACE = "urn:schemas-microsoft-com:office:excel"

ANCHORS = frozenset({"twoCellAnchor", "oneCellAnchor", "absoluteAnchor"})
ANCHOR_LAYOUT = frozenset({"from", "to", "pos", "ext", "clientData"})
NONVISUAL: dict[str, str] = {
    "sp": "nvSpPr",
    "grpSp": "nvGrpSpPr",
    "graphicFrame": "nvGraphicFramePr",
    "cxnSp": "nvCxnSpPr",
    "pic": "nvPicPr",
}
GROUP_PROPERTIES = frozenset({"nvGrpSpPr", "grpSpPr"})
GRAPHIC_KINDS: dict[str, str] = {
    "http://schemas.openxmlformats.org/drawingml/2006/chart": "Chart",
    "http://purl.oclc.org/ooxml/drawingml/chart": "Chart",
    "http://schemas.microsoft.com/office/drawing/2014/chartex": "Chart",
    "http://schemas.openxmlformats.org/drawingml/2006/diagram": "SmartArt graphic",
    "http://purl.oclc.org/ooxml/drawingml/diagram": "SmartArt graphic",
}
KIND_LABELS: dict[str, str] = {
    "pic": "Picture",
    "sp": "Shape",
    "cxnSp": "Connector",
    "grpSp": "Group",
}


@dataclass(frozen=True)
class DrawingObject:
    part: str
    kind: str
    object_id: str | None
    cnvpr_path: XmlPath
    name: str
    title: str | None
    descr: str | None
    decorative: Decorative
    in_group: bool


@dataclass(frozen=True)
class UnsupportedObject:
    part: str
    element_path: XmlPath  # () means the whole part
    label: str
    limitation: str
    cell_ref: str | None = None  # set for rich-value cells; targets use #cell= when present


@dataclass
class DrawingInventory:
    objects: list[DrawingObject]
    unsupported: list[UnsupportedObject]
    limitations: list[str]


def indexed_children(elem: Element, path: XmlPath) -> Iterator[tuple[Element, XmlPath]]:
    counts: dict[str, int] = {}
    for child in elem:
        name = local_name(child.tag)
        counts[name] = counts.get(name, 0) + 1
        yield child, (*path, (name, counts[name]))


def _choose_branch(alternate: Element, path: XmlPath) -> Iterator[tuple[Element, XmlPath]]:
    """First mc:Choice if present, else mc:Fallback. Both describe the same object."""
    branches = [
        (child, child_path)
        for child, child_path in indexed_children(alternate, path)
        if namespace(child.tag) == MC_NAMESPACE
    ]
    for wanted in ("Choice", "Fallback"):
        for child, child_path in branches:
            if local_name(child.tag) == wanted:
                yield from indexed_children(child, child_path)
                return


def _is_alternate(elem: Element) -> bool:
    return elem.tag == f"{{{MC_NAMESPACE}}}AlternateContent"


def _decorative_state(cnvpr: Element) -> Decorative:
    values = [
        node.get("val")
        for ext_list in cnvpr
        if local_name(ext_list.tag) == "extLst"
        for node in ext_list.iter(DECORATIVE_TAG)
    ]
    if not values:
        return "absent"
    normalized = {value.strip() if value is not None else None for value in values}
    if normalized <= {"1", "true"}:
        return "true"
    if normalized <= {"0", "false"}:
        return "false"
    return "ambiguous"


def _graphic_kind(frame: Element) -> str:
    for node in frame.iter():
        if local_name(node.tag) == "graphicData":
            return GRAPHIC_KINDS.get(node.get("uri", ""), "Graphic object")
    return "Graphic object"


class _DrawingWalker:
    def __init__(self, part: str) -> None:
        self.part = part
        self.objects: list[DrawingObject] = []
        self.unsupported: list[UnsupportedObject] = []

    def unsupported_element(self, path: XmlPath, label: str, detail: str) -> None:
        self.unsupported.append(
            UnsupportedObject(self.part, path, label, f"{label} in {self.part}: {detail}")
        )

    def walk_root(self, root: Element) -> None:
        root_path: XmlPath = ((local_name(root.tag), 1),)
        for child, path in indexed_children(root, root_path):
            self.container_child(child, path)

    def container_child(self, elem: Element, path: XmlPath) -> None:
        name = local_name(elem.tag)
        if _is_alternate(elem):
            for child, child_path in _choose_branch(elem, path):
                self.container_child(child, child_path)
        elif name in ANCHORS and namespace(elem.tag) in XDR_NAMESPACES:
            for child, child_path in indexed_children(elem, path):
                if local_name(child.tag) not in ANCHOR_LAYOUT:
                    self.object_slot(child, child_path, in_group=False)
        elif name != "extLst":
            self.unsupported_element(
                path, f"Unrecognized drawing element {name[:40]}", "not inspected for alt text."
            )

    def object_slot(self, elem: Element, path: XmlPath, *, in_group: bool) -> None:
        name = local_name(elem.tag)
        if _is_alternate(elem):
            for child, child_path in _choose_branch(elem, path):
                self.object_slot(child, child_path, in_group=in_group)
            return
        if namespace(elem.tag) not in XDR_NAMESPACES or name not in NONVISUAL:
            if name == "contentPart":
                self.unsupported_element(
                    path, "Ink or content part object", "alt text metadata is not inspected."
                )
            elif name != "extLst":
                self.unsupported_element(
                    path, f"Unrecognized drawing object {name[:40]}", "not inspected."
                )
            return
        self.record(elem, name, path, in_group)
        if name == "grpSp":
            for child, child_path in indexed_children(elem, path):
                if local_name(child.tag) not in GROUP_PROPERTIES:
                    self.object_slot(child, child_path, in_group=True)

    def record(self, elem: Element, name: str, path: XmlPath, in_group: bool) -> None:
        nonvisual = [
            (child, child_path)
            for child, child_path in indexed_children(elem, path)
            if local_name(child.tag) == NONVISUAL[name]
        ]
        cnvpr = [
            (child, child_path)
            for nv, nv_path in nonvisual[:1]
            for child, child_path in indexed_children(nv, nv_path)
            if local_name(child.tag) == "cNvPr"
        ]
        if not cnvpr:
            self.unsupported_element(
                path, "Drawing object without nonvisual properties", "alt text unavailable."
            )
            return
        props, props_path = cnvpr[0]
        kind = KIND_LABELS.get(name, "")
        if name == "graphicFrame":
            kind = _graphic_kind(elem)
        elif name == "sp" and _is_text_box(nonvisual[0][0]):
            kind = "Text box"
        self.objects.append(
            DrawingObject(
                part=self.part,
                kind=kind,
                object_id=props.get("id"),
                cnvpr_path=props_path,
                name=props.get("name", ""),
                title=props.get("title"),
                descr=props.get("descr"),
                decorative=_decorative_state(props),
                in_group=in_group,
            )
        )


def _is_text_box(nonvisual: Element) -> bool:
    for child in nonvisual:
        if local_name(child.tag) == "cNvSpPr":
            return child.get("txBox", "").strip() in ("1", "true")
    return False


def inventory_drawing(package: OoxmlPackage, part: str) -> DrawingInventory:
    root = package.parse_xml(part)
    walker = _DrawingWalker(part)
    if local_name(root.tag) != "wsDr" or namespace(root.tag) not in XDR_NAMESPACES:
        walker.unsupported_element((), "Unrecognized drawing part", "root element not wsDr.")
    else:
        walker.walk_root(root)
    return DrawingInventory(walker.objects, walker.unsupported, [])


def inventory_vml(package: OoxmlPackage, part: str) -> DrawingInventory:
    """Legacy VML: comment notes are disclosed; every other shape needs manual review."""
    try:
        root = package.parse_xml(part)
    except WorkbookValidationError:
        return DrawingInventory(
            [],
            [
                UnsupportedObject(
                    part,
                    (),
                    "Unreadable legacy VML drawing",
                    f"Legacy VML part {part} could not be read safely; its objects were not "
                    "inspected for alt text.",
                )
            ],
            [],
        )
    unsupported: list[UnsupportedObject] = []
    notes = 0
    root_path: XmlPath = ((local_name(root.tag), 1),)
    for child, path in indexed_children(root, root_path):
        name = local_name(child.tag)
        if namespace(child.tag) != VML_NAMESPACE or name == "shapetype":
            continue
        object_type = ""
        for data in child.iter(f"{{{VML_EXCEL_NAMESPACE}}}ClientData"):
            object_type = data.get("ObjectType", "")
            break
        if object_type == "Note":
            notes += 1
            continue
        label = f"Legacy VML {(object_type or name)[:40]} object"
        unsupported.append(
            UnsupportedObject(
                part,
                path,
                label,
                f"{label} in {part}: VML alt text is not interpreted (partial support).",
            )
        )
    limitations = []
    if notes:
        limitations.append(
            f"{notes} cell comment note shape(s) in legacy VML part {part} were treated as "
            "cell comments, not drawing objects, and were not checked for alt text."
        )
    return DrawingInventory([], unsupported, limitations)
