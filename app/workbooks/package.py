"""Bounded, read-only OOXML package access over immutable bytes.

Nothing is extracted to disk. Entry names are checked before any read. Every read counts
actual decompressed bytes against per-part and whole-package budgets.
"""

import io
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from posixpath import dirname
from typing import IO
from urllib.parse import unquote
from xml.etree.ElementTree import Element

from app.contracts.models import ProcessingLimits
from app.workbooks import errors
from app.workbooks.xmlsafe import (
    EndHandler,
    MalformedXmlError,
    StartHandler,
    UnsafeXmlError,
    local_name,
    parse_document,
    stream_document,
)

ZIP_MAGIC = b"PK\x03\x04"
CFB_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
CONTENT_TYPES_PART = "[Content_Types].xml"
ROOT_RELS_PART = "_rels/.rels"
REL_PREFIXES = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/",
    "http://purl.oclc.org/ooxml/officeDocument/relationships/",
)
XML_EXTENSIONS = (".xml", ".rels")
_READ_CHUNK = 64 * 1024


class PackageLimitError(Exception):
    def __init__(self, limit: str, maximum: int) -> None:
        super().__init__(limit)
        self.limit = limit
        self.maximum = maximum


class UnsafeTargetError(Exception):
    """A relationship target escapes the package root."""


@dataclass(frozen=True)
class Relationship:
    rel_id: str
    rel_type: str
    target: str
    external: bool
    resolved: str | None  # stored ZIP entry name, or None if external/missing


def rel_type_is(rel_type: str, suffix: str) -> bool:
    return any(rel_type == prefix + suffix for prefix in REL_PREFIXES)


def rels_part_for(part: str) -> str:
    """Relationship part owned by `part` ("" is the package root)."""
    if not part:
        return ROOT_RELS_PART
    folder, _, name = part.rpartition("/")
    return f"{folder}/_rels/{name}.rels" if folder else f"_rels/{name}.rels"


def owner_folder_of_rels(rels_part: str) -> str:
    folder = dirname(rels_part)
    if folder == "_rels":
        return ""
    if folder.endswith("/_rels"):
        return folder[: -len("/_rels")]
    return folder


def check_entry_name(name: str) -> None:
    if not name or name.startswith("/") or "\\" in name or ":" in name:
        errors.unsafe("entry_path")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        errors.unsafe("entry_path")
    segments = name.split("/")
    if name.endswith("/"):
        segments = segments[:-1]
    if any(segment in ("", ".", "..") for segment in segments):
        errors.unsafe("entry_path")


class _BoundedReader:
    """File-like wrapper that enforces actual decompressed byte budgets."""

    def __init__(self, inner: IO[bytes], package: "OoxmlPackage", part_limit: int) -> None:
        self._inner = inner
        self._package = package
        self._part_limit = part_limit
        self._read = 0

    def read(self, size: int = -1) -> bytes:
        want = _READ_CHUNK if size is None or size < 0 else min(size, _READ_CHUNK)
        chunk = self._inner.read(want)
        self._read += len(chunk)
        if self._read > self._part_limit:
            raise PackageLimitError("max_xml_part_bytes", self._part_limit)
        self._package.charge(len(chunk))
        return chunk


@dataclass
class OoxmlPackage:
    """Open an untrusted package. Construction performs all central-directory checks."""

    data: bytes
    limits: ProcessingLimits
    _zip: zipfile.ZipFile = field(init=False, repr=False)
    _names: dict[str, str] = field(init=False, default_factory=dict)  # casefold -> stored
    _expanded: int = field(init=False, default=0)
    _content_defaults: dict[str, str] = field(init=False, default_factory=dict)
    _content_overrides: dict[str, str] = field(init=False, default_factory=dict)
    parsed_parts: set[str] = field(init=False, default_factory=set)
    _rels_cache: dict[str, dict[str, Relationship]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        if self.data.startswith(CFB_MAGIC):
            if "EncryptionInfo".encode("utf-16-le") in self.data:
                errors.reject(
                    422,
                    errors.ENCRYPTED,
                    "Encrypted or password-protected workbooks are not supported.",
                    {"reason": "ole_encrypted_package"},
                )
            errors.invalid("ole_compound_file", "The file is a legacy binary, not an .xlsx.")
        if not self.data.startswith(ZIP_MAGIC):
            errors.invalid("not_zip")
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(self.data), "r")
            infos = self._zip.infolist()
        except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, EOFError):
            errors.invalid("corrupt_zip")
        if len(infos) > self.limits.max_zip_entries:
            errors.limit_exceeded("max_zip_entries", self.limits.max_zip_entries)
        declared = 0
        for info in infos:
            check_entry_name(info.filename)
            key = info.filename.casefold()
            if key in self._names:
                errors.unsafe("duplicate_entry")
            self._names[key] = info.filename
            if info.flag_bits & 0x1:
                errors.reject(
                    422,
                    errors.ENCRYPTED,
                    "Encrypted or password-protected workbooks are not supported.",
                    {"reason": "encrypted_zip_entry"},
                )
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                errors.invalid("unsupported_compression")
            if info.filename.lower().endswith(XML_EXTENSIONS):
                if info.file_size > self.limits.max_xml_part_bytes:
                    errors.limit_exceeded("max_xml_part_bytes", self.limits.max_xml_part_bytes)
            declared += info.file_size
            if declared > self.limits.max_expanded_bytes:
                errors.limit_exceeded("max_expanded_bytes", self.limits.max_expanded_bytes)
        self._load_content_types()

    # -- budget and raw access -------------------------------------------------

    def charge(self, count: int) -> None:
        self._expanded += count
        if self._expanded > self.limits.max_expanded_bytes:
            raise PackageLimitError("max_expanded_bytes", self.limits.max_expanded_bytes)

    @property
    def entry_names(self) -> list[str]:
        return sorted(name for name in self._names.values() if not name.endswith("/"))

    def find(self, part: str) -> str | None:
        """Stored entry name for a part, matched ASCII case-insensitively like OPC."""
        stored = self._names.get(part.casefold())
        return stored if stored is not None and not stored.endswith("/") else None

    def _open(self, stored: str) -> _BoundedReader:
        try:
            inner = self._zip.open(stored, "r")
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, KeyError, ValueError):
            errors.invalid("unreadable_entry")
        return _BoundedReader(inner, self, self.limits.max_xml_part_bytes)

    def _guard[T](self, stored: str, action: Callable[[_BoundedReader], T]) -> T:
        try:
            result = action(self._open(stored))
        except PackageLimitError as exc:
            errors.limit_exceeded(exc.limit, exc.maximum)
        except UnsafeXmlError:
            errors.unsafe("xml_dtd_or_entity")
        except MalformedXmlError:
            errors.invalid("malformed_xml")
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError):
            errors.invalid("corrupt_entry")
        self.parsed_parts.add(stored)
        return result

    def parse_xml(self, stored: str) -> Element:
        return self._guard(stored, parse_document)

    def stream_xml(
        self, stored: str, on_start: StartHandler | None = None, on_end: EndHandler | None = None
    ) -> str:
        return self._guard(stored, lambda reader: stream_document(reader, on_start, on_end))

    # -- content types and relationships ----------------------------------------

    def _load_content_types(self) -> None:
        stored = self.find(CONTENT_TYPES_PART)
        if stored is None:
            errors.invalid("missing_content_types")
        root = self.parse_xml(stored)
        if root.tag != f"{{{CONTENT_TYPES_NS}}}Types":
            errors.invalid("bad_content_types")
        for child in root:
            name = local_name(child.tag)
            content_type = child.get("ContentType", "")
            if name == "Default":
                self._content_defaults[child.get("Extension", "").casefold()] = content_type
            elif name == "Override":
                part = child.get("PartName", "").lstrip("/")
                self._content_overrides[unquote(part).casefold()] = content_type

    def content_type(self, stored: str) -> str | None:
        override = self._content_overrides.get(stored.casefold())
        if override is not None:
            return override
        extension = stored.rpartition(".")[2].casefold() if "." in stored else ""
        return self._content_defaults.get(extension)

    @property
    def all_content_types(self) -> list[str]:
        return sorted(set(self._content_defaults.values()) | set(self._content_overrides.values()))

    def resolve_target(self, owner_folder: str, target: str) -> str:
        """Normalize an internal target relative to its owning part's folder.

        Raises UnsafeTargetError when the target would leave the package root.
        """
        path = unquote(target.split("#", 1)[0])
        if "\\" in path:
            raise UnsafeTargetError(target)
        segments: list[str] = [] if path.startswith("/") else owner_folder.split("/")
        for segment in path.split("/"):
            if segment in ("", "."):
                continue
            if segment == "..":
                if not segments or segments == [""]:
                    raise UnsafeTargetError(target)
                segments.pop()
                continue
            segments.append(segment)
        return "/".join(segment for segment in segments if segment)

    def relationships(self, owner_part: str) -> dict[str, Relationship]:
        """Relationships of a part keyed by Id; {} when the part has no .rels."""
        stored = self.find(rels_part_for(owner_part))
        if stored is None:
            return {}
        return self.parse_rels(stored)

    def parse_rels(self, stored_rels: str) -> dict[str, Relationship]:
        cached = self._rels_cache.get(stored_rels)
        if cached is not None:
            return cached
        root = self.parse_xml(stored_rels)
        if root.tag != f"{{{RELS_NS}}}Relationships":
            errors.invalid("bad_relationships")
        folder = owner_folder_of_rels(stored_rels)
        result: dict[str, Relationship] = {}
        for child in root:
            if child.tag != f"{{{RELS_NS}}}Relationship":
                continue
            rel_id = child.get("Id", "")
            target = child.get("Target", "")
            if not rel_id or rel_id in result:
                errors.invalid("bad_relationship_id")
            external = child.get("TargetMode", "Internal") == "External"
            resolved: str | None = None
            if not external:
                try:
                    resolved = self.find(self.resolve_target(folder, target))
                except UnsafeTargetError:
                    errors.unsafe("relationship_target")
            result[rel_id] = Relationship(rel_id, child.get("Type", ""), target, external, resolved)
        self._rels_cache[stored_rels] = result
        return result
