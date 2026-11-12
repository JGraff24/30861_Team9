"""Validate wire bodies against an adopted pod JSON Schema pack.

The tester shares wire *schemas* with the system (allowed by §15) but validates them with its own
JSON Schema 2020-12 validator, never with the system's DTOs. Semantic rules that the schema pack
leaves to application validation (ownership, ordering, coherence) stay in the check handlers.

Phase 4 runs validate against specification 2.1.0. A Phase 5 run selects the 3.0.0 pack for the
whole run (`use_pack("phase5")`), so inherited Phase 4 checks see the retained definitions plus
the explicit Phase 5 overrides: new audit reports carry specificationVersion 3.0.0 and
timestamps have at most six fractional digits. Earlier stored reports are never re-validated.
"""

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]

PackName = Literal["phase4", "phase5"]
SCHEMA_FILE = "phase4-wire-schemas.json"
_ROOT = Path(__file__).resolve().parents[2]
_FILES: dict[PackName, tuple[Path, Path, str]] = {
    "phase4": (
        Path(__file__).resolve().parent / "phase4-wire-schemas.json",
        _ROOT / "docs" / "contracts" / "phase4" / "phase4-wire-schemas.json",
        "2.1.0",
    ),
    "phase5": (
        _ROOT / "tester" / "phase5" / "phase5-wire-schemas.json",
        _ROOT / "docs" / "contracts" / "phase5" / "phase5-wire-schemas.json",
        "3.0.0",
    ),
}
# In a Phase 5 run every report the target produces is new, so it must be the 3.0.0 profile;
# the retained legacy profile only describes reports stored before the migration.
_ALIASES: dict[PackName, dict[str, str]] = {
    "phase4": {},
    "phase5": {"AuditReport": "NewAuditReport"},
}
SPECIFICATION_VERSION = "2.1.0"
RULESET_VERSION = "2.0.0"
REPORT_VERSION = "2.0.0"
_ACTIVE: ContextVar[PackName] = ContextVar("tester_wire_pack", default="phase4")


class ContractViolation(ValueError):
    """A body does not satisfy the named definition of the frozen pack."""


def active_pack() -> PackName:
    return _ACTIVE.get()


@contextmanager
def use_pack(name: PackName) -> Iterator[None]:
    """Select the pod pack for every validation in this context (one suite run)."""
    token = _ACTIVE.set(name)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def specification_version(name: PackName | None = None) -> str:
    return _FILES[name or active_pack()][2]


def schema_path(name: PackName | None = None) -> Path:
    """The wheel ships a copy beside the tester package; a checkout reads the adopted pack."""
    packaged, repository, _ = _FILES[name or active_pack()]
    return packaged if packaged.exists() else repository


RFC3339_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z")


def parse_timestamp(value: str) -> datetime:
    """Strict UTC RFC 3339 (`YYYY-MM-DDTHH:MM:SS[.fraction]Z`) to an aware datetime."""
    if not RFC3339_UTC.fullmatch(value):
        raise ValueError("not a UTC RFC 3339 timestamp ending in Z")
    whole, _, fraction = value[:-1].partition(".")
    parsed = datetime.fromisoformat(whole + "+00:00")
    if fraction:
        parsed = parsed.replace(microsecond=int((fraction + "000000")[:6]))
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamp is not UTC")
    return parsed


def _rfc3339(value: object) -> bool:
    if not isinstance(value, str):
        return True
    try:
        parse_timestamp(value)
    except ValueError:
        return False
    return True


def _calendar_date(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


_FORMATS = FormatChecker(formats=())
_FORMATS.checks("date-time")(_rfc3339)
_FORMATS.checks("date")(_calendar_date)


@cache
def _pack(name: PackName) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(schema_path(name).read_text(encoding="utf-8"))
    version = specification_version(name)
    if not document.get("$id", "").endswith(f"-{version}.json") and not document.get(
        "$id", ""
    ).endswith(f":{version}"):
        raise RuntimeError(f"the packaged wire schemas are not specification {version}")
    Draft202012Validator.check_schema(document)
    return document


def pack() -> dict[str, Any]:
    return _pack(active_pack())


def definitions() -> list[str]:
    return sorted(pack()["$defs"])


@cache
def _validator(name: PackName, definition: str) -> Draft202012Validator:
    document = _pack(name)
    definition = _ALIASES[name].get(definition, definition)
    if definition not in document["$defs"]:
        raise KeyError(f"unknown {name} definition {definition}")
    schema = {
        "$schema": document["$schema"],
        "$id": document["$id"],
        "$defs": document["$defs"],
        "$ref": f"#/$defs/{definition}",
    }
    return Draft202012Validator(schema, format_checker=_FORMATS)


def validator(definition: str) -> Draft202012Validator:
    return _validator(active_pack(), definition)


def violations(definition: str, body: object) -> list[str]:
    """Every schema violation, as short stable descriptions (paths only, no body contents)."""
    found = []
    for error in sorted(validator(definition).iter_errors(body), key=lambda e: list(e.path)):
        location = "/".join(str(part) for part in error.absolute_path) or "(root)"
        found.append(f"{location}: {error.validator} constraint")
    return found


def validate(definition: str, body: object) -> None:
    problems = violations(definition, body)
    if problems:
        raise ContractViolation(f"body does not match {definition}: {problems[0]}")


def parse_json(content: bytes, definition: str) -> Any:
    """Strict UTF-8 JSON (no duplicate keys, no NaN) validated against one definition."""

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ContractViolation(f"body for {definition} repeats an object key")
        return dict(pairs)

    def no_constant(token: str) -> Any:
        raise ContractViolation(f"body for {definition} contains {token}")

    try:
        text = content.decode("utf-8")
        body = json.loads(text, object_pairs_hook=no_duplicates, parse_constant=no_constant)
    except ContractViolation:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ContractViolation(f"body for {definition} is not UTF-8 JSON") from None
    except (RecursionError, ValueError):
        # Pathological nesting or oversized integers are target defects, not tester faults.
        raise ContractViolation(f"body for {definition} is not acceptable JSON") from None
    try:
        validate(definition, body)
    except RecursionError:
        raise ContractViolation(f"body for {definition} is nested too deeply") from None
    return body
