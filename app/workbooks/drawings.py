"""Drawing object inventory read directly from DrawingML and legacy VML parts.

Library object models omit shapes and alt-text metadata, so the XML is walked directly.
Representations that cannot be inspected become UnsupportedObject records; they are
never dropped.
"""

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal
from xml.etree.ElementTree import Element

from app.contracts.errors import WorkbookValidationError
from app.workbooks.package import OoxmlPackage, Relationship, rel_type_is
from app.workbooks.structure import r_attr
from app.workbooks.xmlsafe import local_name, namespace

XmlPath = tuple[tuple[str, int], ...]
Decorative = Literal["absent", "true", "false", "ambiguous"]

XDR_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
        "http://purl.oclc.org/ooxml/drawingml/spreadsheetDrawing",
    }
)
CHART_TAGS = frozenset(
    {
        "{http://schemas.openxmlformats.org/drawingml/2006/chart}chart",
        "{http://purl.oclc.org/ooxml/drawingml/chart}chart",
    }
)
MC_NAMESPACE = "http://schemas.openxmlformats.org/markup-compatibility/2006"
DECORATIVE_TAG = "{http://schemas.microsoft.com/office/drawing/2017/decorative}decorative"
DECORATIVE_VALUES: dict[str, Decorative] = {
    "1": "true",
    "true": "true",
    "0": "false",
    "false": "false",
}

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
KIND_LABELS: dict[str, str] = {
    "pic": "Picture",
    "sp": "Shape",
    "cxnSp": "Connector",
    "grpSp": "Group",
    "graphicFrame": "Chart",
}


@dataclass(frozen=True)
class DrawingObject:
    """An inspectable object with a unique cNvPr id (its target is <part>#id=<id>)."""

    part: str
    kind: str
    object_id: str
    name: str
    title: str | None
    descr: str | None
    decorative: Decorative


@dataclass(frozen=True)
class UnsupportedObject:
    """A representation sent to manual review instead of being inspected.

    Target: <part>#id=<object_id> when the object has a unique id, <part>#xml=/ for a
    package-level part, <part>#xml=<element_path> otherwise, or the bare part when the
    path is empty.
    """

    part: str
    element_path: XmlPath
    label: str
    limitation: str
    object_id: str | None = None
    part_root: bool = False


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


def element_paths(root: Element) -> dict[Element, XmlPath]:
    """One-based, namespace-independent paths for every element, from the root."""
    paths: dict[Element, XmlPath] = {root: ((local_name(root.tag), 1),)}
    pending = [root]
    while pending:
        elem = pending.pop()
        for child, child_path in indexed_children(elem, paths[elem]):
            paths[child] = child_path
            pending.append(child)
    return paths


def _decorative_state(cnvpr: Element) -> Decorative:
    """Exactly one native decorative marker with val 0/1/false/true; anything else is ambiguous."""
    markers = [node for node in cnvpr.iter() if local_name(node.tag) == "decorative"]
    if not markers:
        return "absent"
    if len(markers) != 1 or markers[0].tag != DECORATIVE_TAG:
        return "ambiguous"
    return DECORATIVE_VALUES.get(markers[0].get("val") or "", "ambiguous")


class _DrawingWalker:
    def __init__(self, package: OoxmlPackage, part: str, root: Element) -> None:
        self.package = package
        self.part = part
        self.objects: list[DrawingObject] = []
        self.unsupported: list[UnsupportedObject] = []
        self.id_counts = Counter(
            object_id
            for node in root.iter()
            if local_name(node.tag) == "cNvPr" and namespace(node.tag) == namespace(root.tag)
            if (object_id := node.get("id"))
        )
        self._rels: dict[str, Relationship] | None = None

    def rels(self) -> dict[str, Relationship]:
        if self._rels is None:
            self._rels = self.package.relationships(self.part)
        return self._rels

    def review(self, path: XmlPath, label: str, detail: str, object_id: str | None = None) -> None:
        self.unsupported.append(
            UnsupportedObject(
                self.part, path, label, f"{label} in {self.part}: {detail}", object_id
            )
        )

    def walk_root(self, root: Element) -> None:
        root_path: XmlPath = ((local_name(root.tag), 1),)
        for child, path in indexed_children(root, root_path):
            name = local_name(child.tag)
            if name in ANCHORS and namespace(child.tag) in XDR_NAMESPACES:
                self.children(child, path, ANCHOR_LAYOUT)
            elif _is_alternate(child):
                self.review(path, "Alternate content", "alternative representations are reviewed.")
            elif name != "extLst":
                self.review(path, f"Unrecognized drawing element {name[:40]}", "not inspected.")

    def children(self, parent: Element, path: XmlPath, skip: frozenset[str]) -> None:
        for child, child_path in indexed_children(parent, path):
            if local_name(child.tag) not in skip:
                self.object_slot(child, child_path)

    def object_slot(self, elem: Element, path: XmlPath) -> None:
        name = local_name(elem.tag)
        if _is_alternate(elem):
            self.review(path, "Alternate content", "alternative representations are reviewed.")
            return
        if namespace(elem.tag) not in XDR_NAMESPACES or name not in NONVISUAL:
            if name == "contentPart":
                self.review(path, "Ink or content part object", "alt text is not inspected.")
            elif name != "extLst":
                self.review(path, f"Unrecognized drawing object {name[:40]}", "not inspected.")
            return
        cnvpr = [
            child
            for nv in elem
            if local_name(nv.tag) == NONVISUAL[name] and namespace(nv.tag) == namespace(elem.tag)
            for child in nv
            if local_name(child.tag) == "cNvPr" and namespace(child.tag) == namespace(elem.tag)
        ]
        if len(cnvpr) != 1:
            self.review(
                path, "Drawing object without unique nonvisual properties", "not inspected."
            )
            return
        props = cnvpr[0]
        object_id = props.get("id")
        if not object_id or self.id_counts[object_id] != 1:
            self.review(path, "Drawing object with a missing or duplicate id", "not inspected.")
            if name == "grpSp":
                self.children(elem, path, GROUP_PROPERTIES)
            return
        decorative = _decorative_state(props)
        label = KIND_LABELS[name]
        if name == "sp" and _is_text_box(elem):
            label = "Text box"
        if name == "grpSp":
            # A group is a container: its children are checked individually. Only an
            # explicit decorative designation on the group itself needs confirmation.
            if decorative != "absent" and decorative != "false":
                self.record(props, label, object_id, decorative)
            self.children(elem, path, GROUP_PROPERTIES)
            return
        if name == "graphicFrame" and not self.is_chart(elem):
            self.review(
                path,
                "Non-chart graphic frame",
                "SmartArt and other graphics are reviewed.",
                object_id,
            )
            return
        if self.has_external_content(elem):
            self.review(
                path,
                f"{label} with external content",
                "external content is not fetched.",
                object_id,
            )
            return
        self.record(props, label, object_id, decorative)

    def record(self, props: Element, label: str, object_id: str, decorative: Decorative) -> None:
        self.objects.append(
            DrawingObject(
                self.part,
                label,
                object_id,
                props.get("name", ""),
                props.get("title"),
                props.get("descr"),
                decorative,
            )
        )

    def is_chart(self, frame: Element) -> bool:
        data = [node for node in frame.iter() if local_name(node.tag) == "graphicData"]
        if len(data) != 1 or len(data[0]) != 1 or data[0][0].tag not in CHART_TAGS:
            return False
        rel = self.rels().get(r_attr(data[0][0], "id") or "")
        return rel is not None and rel_type_is(rel.rel_type, "chart") and not rel.external

    def has_external_content(self, elem: Element) -> bool:
        for node in elem.iter():
            for key, value in node.attrib.items():
                if key.startswith("{") and local_name(key) in ("link", "embed"):
                    rel = self.rels().get(value)
                    if rel is not None and rel.external:
                        return True
        return False


def _is_text_box(shape: Element) -> bool:
    for nonvisual in shape:
        if local_name(nonvisual.tag) == "nvSpPr":
            for child in nonvisual:
                if local_name(child.tag) == "cNvSpPr":
                    return child.get("txBox", "").strip() in ("1", "true")
    return False


def _is_alternate(elem: Element) -> bool:
    return elem.tag == f"{{{MC_NAMESPACE}}}AlternateContent"


def inventory_drawing(package: OoxmlPackage, part: str) -> DrawingInventory:
    root = package.parse_xml(part)
    walker = _DrawingWalker(package, part, root)
    if local_name(root.tag) != "wsDr" or namespace(root.tag) not in XDR_NAMESPACES:
        walker.review(((local_name(root.tag), 1),), "Unrecognized drawing part", "not wsDr.")
    else:
        walker.walk_root(root)
    return DrawingInventory(walker.objects, walker.unsupported, [])


def inventory_vml(package: OoxmlPackage, part: str) -> DrawingInventory:
    """Legacy VML: every shape (comment notes included) needs manual review.

    Shapes with a unique id are targeted by #id=; others by their element path. An empty
    inventory means the part has no shapes and the caller reviews the referencing element.
    """
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
    paths = element_paths(root)
    shapes = [node for node in root.iter() if local_name(node.tag) == "shape"]
    ids = Counter(shape.get("id") for shape in shapes)
    limitation = f"Legacy VML part {part}: VML objects are not interpreted (partial support)."
    unsupported = []
    for shape in shapes:
        shape_id = shape.get("id")
        unique = shape_id if shape_id and ids[shape_id] == 1 else None
        unsupported.append(
            UnsupportedObject(part, paths[shape], "Legacy VML object", limitation, unique)
        )
    return DrawingInventory([], unsupported, [])
