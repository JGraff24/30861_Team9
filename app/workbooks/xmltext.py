"""Targeted lexical edits of UTF-8 XML parts.

A general load/save cycle would rename namespace prefixes, drop markup-compatibility
declarations or reorder attributes. Instead, the editor locates elements by their indexed
local-name path and replaces only the exact character spans an approved correction
changes. Every other byte of the part is copied unchanged.

Input parts have already passed the hardened parser, so they are well-formed and contain
no DTD. The tokenizer still refuses anything it does not recognize.
"""

import re
from dataclasses import dataclass, field

XmlPath = tuple[tuple[str, int], ...]
XML_NS = "http://www.w3.org/XML/1998/namespace"
_BOM = "﻿"
_TOKEN = re.compile(
    r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>"
    r"|<(/?)([^\s/>!?]+)((?:\s+[^\s=/>]+\s*=\s*(?:\"[^\"]*\"|'[^']*'))*)\s*(/?)>",
    re.DOTALL,
)
_ATTRIBUTE = re.compile(r"([^\s=/>]+)\s*=\s*(\"[^\"]*\"|'[^']*')")
_DECLARATION = re.compile(r"^<\?xml[^>]*encoding\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_REFERENCE = re.compile(r"&(#x[0-9A-Fa-f]+|#[0-9]+|amp|lt|gt|quot|apos);")
_NAMED = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}


class NotEditable(Exception):
    """The part cannot be edited with exact span preservation."""


def unescape(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token.startswith("#x"):
            return chr(int(token[2:], 16))
        if token.startswith("#"):
            return chr(int(token[1:]))
        return _NAMED[token]

    return _REFERENCE.sub(replace, text)


def escape_attribute(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("\t", "&#9;")
        .replace("\n", "&#10;")
        .replace("\r", "&#13;")
    )


def escape_text(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\r", "&#13;")
    )


def editable_encoding(raw: bytes) -> bool:
    """UTF-8 parts (optionally with a BOM) are the only ones edited in place."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    head = raw[3:] if raw.startswith(b"\xef\xbb\xbf") else raw
    match = _DECLARATION.match(head[:200].decode("ascii", "replace"))
    if match is not None and match.group(1).strip().lower() not in ("utf-8", "utf8"):
        return False
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


@dataclass
class Node:
    qname: str
    local: str
    prefix: str
    start: int
    start_end: int
    attrs_end: int  # where a new attribute is inserted
    self_closing: bool
    path: XmlPath
    parent: int | None
    attributes: dict[str, tuple[str, int, int]]  # qname -> (raw value, span start, span end)
    declarations: dict[str, str]  # prefix ("" for default) -> namespace URI
    end: int = -1  # start of the end tag (== start for self-closing)
    end_end: int = -1
    children: list[int] = field(default_factory=list)

    def attribute(self, qname: str) -> str | None:
        found = self.attributes.get(qname)
        return None if found is None else unescape(found[0])


class XmlText:
    def __init__(self, raw: bytes) -> None:
        if not editable_encoding(raw):
            raise NotEditable("not UTF-8")
        self.bom = raw.startswith(b"\xef\xbb\xbf")
        self.text = raw.decode("utf-8-sig" if self.bom else "utf-8")
        self.nodes: list[Node] = []
        self.by_path: dict[XmlPath, Node] = {}
        self._edits: list[tuple[int, int, int, str]] = []
        self._tokenize()

    def _tokenize(self) -> None:
        stack: list[int] = []
        counters: list[dict[str, int]] = [{}]
        position = 0
        text = self.text
        while True:
            position = text.find("<", position)
            if position < 0:
                break
            match = _TOKEN.match(text, position)
            if match is None:
                raise NotEditable("unrecognized markup")
            position = match.end()
            name = match.group(2)
            if name is None:
                continue
            if match.group(1):
                if not stack or self.nodes[stack[-1]].qname != name:
                    raise NotEditable("unbalanced end tag")
                node = self.nodes[stack.pop()]
                node.end, node.end_end = match.start(), match.end()
                counters.pop()
                continue
            prefix, _, local = name.rpartition(":")
            siblings = counters[-1]
            siblings[local] = siblings.get(local, 0) + 1
            parent = stack[-1] if stack else None
            parent_path = self.nodes[parent].path if parent is not None else ()
            attributes: dict[str, tuple[str, int, int]] = {}
            declarations: dict[str, str] = {}
            offset = match.start(3)
            for attribute in _ATTRIBUTE.finditer(match.group(3)):
                qname, quoted = attribute.group(1), attribute.group(2)
                value = quoted[1:-1]
                attributes[qname] = (value, offset + attribute.start(2), offset + attribute.end(2))
                if qname == "xmlns":
                    declarations[""] = unescape(value)
                elif qname.startswith("xmlns:"):
                    declarations[qname[6:]] = unescape(value)
            node = Node(
                name,
                local,
                prefix,
                match.start(),
                match.end(),
                match.end(3),
                bool(match.group(4)),
                (*parent_path, (local, siblings[local])),
                parent,
                attributes,
                declarations,
            )
            index = len(self.nodes)
            self.nodes.append(node)
            if parent is not None:
                self.nodes[parent].children.append(index)
            elif index:
                raise NotEditable("multiple root elements")
            self.by_path[node.path] = node
            if node.self_closing:
                node.end, node.end_end = node.start, node.start_end
            else:
                stack.append(index)
                counters.append({})
        if stack or not self.nodes:
            raise NotEditable("unclosed elements")

    # -- lookup ------------------------------------------------------------------------

    @property
    def root(self) -> Node:
        return self.nodes[0]

    def find(self, path: XmlPath) -> Node:
        node = self.by_path.get(path)
        if node is None:
            raise NotEditable("element not found")
        return node

    def namespaces(self, node: Node) -> dict[str, str]:
        chain: list[Node] = []
        current: Node | None = node
        while current is not None:
            chain.append(current)
            current = self.nodes[current.parent] if current.parent is not None else None
        scope: dict[str, str] = {"xml": XML_NS}
        for item in reversed(chain):
            scope.update(item.declarations)
        return scope

    def uri(self, node: Node) -> str:
        return self.namespaces(node).get(node.prefix, "")

    def prefix_for(self, node: Node, uri: str) -> str | None:
        for prefix, bound in self.namespaces(node).items():
            if bound == uri:
                return prefix
        return None

    def children(self, node: Node) -> list[Node]:
        return [self.nodes[index] for index in node.children]

    def inner(self, node: Node) -> str:
        return "" if node.self_closing else self.text[node.start_end : node.end]

    def text_content(self, node: Node) -> str:
        """Unescaped character content of a text-only element."""
        if node.children:
            raise NotEditable("element has child markup")
        inner = self.inner(node)
        if "<" in inner:
            raise NotEditable("element text has comments or CDATA")
        return unescape(inner)

    # -- edits -------------------------------------------------------------------------

    def _edit(self, start: int, end: int, replacement: str) -> None:
        self._edits.append((start, end, len(self._edits), replacement))

    def set_attribute(self, node: Node, qname: str, value: str) -> None:
        found = node.attributes.get(qname)
        if found is not None:
            self._edit(found[1], found[2], f'"{escape_attribute(value)}"')
        else:
            self._edit(node.attrs_end, node.attrs_end, f' {qname}="{escape_attribute(value)}"')

    def replace_element(self, node: Node, replacement: str) -> None:
        self._edit(node.start, node.end_end, replacement)

    def replace_text(self, node: Node, value: str) -> None:
        if node.self_closing:
            self._edit(
                node.start,
                node.end_end,
                f"{self.text[node.start : node.start_end - 2].rstrip()}>"
                f"{escape_text(value)}</{node.qname}>",
            )
        else:
            self._edit(node.start_end, node.end, escape_text(value))

    def insert(self, offset: int, markup: str) -> None:
        self._edit(offset, offset, markup)

    def append_child(self, node: Node, markup: str) -> None:
        if node.self_closing:
            opening = self.text[node.start : node.start_end - 2].rstrip() + ">"
            self._edit(node.start, node.end_end, f"{opening}{markup}</{node.qname}>")
        else:
            self._edit(node.end, node.end, markup)

    @property
    def changed(self) -> bool:
        return bool(self._edits)

    def render(self) -> bytes:
        ordered = sorted(self._edits)
        pieces: list[str] = []
        cursor = 0
        for start, end, _, replacement in ordered:
            if start < cursor:
                raise NotEditable("overlapping edits")
            pieces.append(self.text[cursor:start])
            pieces.append(replacement)
            cursor = end
        pieces.append(self.text[cursor:])
        body = "".join(pieces).encode("utf-8")
        return (b"\xef\xbb\xbf" + body) if self.bom else body


def qualified(prefix: str, local: str) -> str:
    return f"{prefix}:{local}" if prefix else local
