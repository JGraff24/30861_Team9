"""Hardened XML parsing. Every workbook XML read goes through these functions.

defusedxml rejects DTDs, entity declarations and external references. Streaming parses
drop finished elements so memory stays bounded by the deepest open subtree.
"""

from collections.abc import Callable, Iterator
from typing import Protocol
from xml.etree.ElementTree import Element, ParseError

import defusedxml.ElementTree as _defused  # type: ignore[import-untyped]
from defusedxml import DefusedXmlException


class Readable(Protocol):
    def read(self, size: int = -1) -> bytes: ...


class UnsafeXmlError(Exception):
    """The XML declared a DTD, entity or external reference."""


class MalformedXmlError(Exception):
    """The XML could not be parsed."""


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if tag.startswith("{") else tag


def namespace(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def parse_document(stream: Readable) -> Element:
    """Parse a whole document; callers bound the stream's size."""
    try:
        root: Element = _defused.parse(stream, forbid_dtd=True).getroot()
    except DefusedXmlException as exc:
        raise UnsafeXmlError(type(exc).__name__) from exc
    except ParseError as exc:
        raise MalformedXmlError(str(exc)) from exc
    return root


StartHandler = Callable[[Element, list[Element]], None]
EndHandler = Callable[[Element, list[Element]], bool]


def stream_document(
    stream: Readable,
    on_start: StartHandler | None = None,
    on_end: EndHandler | None = None,
) -> str:
    """Stream-parse a document and return the root element's tag.

    on_start receives each element with its open ancestors (root first). on_end receives
    the finished element and its ancestors and returns True to keep the element attached
    to its parent; otherwise it is detached immediately to bound memory.
    """
    stack: list[Element] = []
    root_tag = ""
    try:
        events: Iterator[tuple[str, Element]] = _defused.iterparse(
            stream, events=("start", "end"), forbid_dtd=True
        )
        for event, elem in events:
            if event == "start":
                if not stack:
                    root_tag = elem.tag
                if on_start is not None:
                    on_start(elem, stack)
                stack.append(elem)
                continue
            stack.pop()
            keep = on_end(elem, stack) if on_end is not None else False
            if stack and not keep:
                stack[-1].remove(elem)
    except DefusedXmlException as exc:
        raise UnsafeXmlError(type(exc).__name__) from exc
    except ParseError as exc:
        raise MalformedXmlError(str(exc)) from exc
    if not root_tag:
        raise MalformedXmlError("empty document")
    return root_tag
