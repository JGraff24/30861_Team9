"""Validate wire bodies against the adopted Phase 4 JSON Schema pack (specification 2.1.0).

The tester shares wire *schemas* with the system (allowed by §15) but validates them with its own
JSON Schema 2020-12 validator, never with the system's DTOs. Semantic rules that the schema pack
leaves to application validation (ownership, ordering, coherence) stay in the check handlers.
"""

import json
import re
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]

SCHEMA_FILE = "phase4-wire-schemas.json"
_PACKAGED = Path(__file__).resolve().parent / SCHEMA_FILE
_REPOSITORY = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "phase4" / SCHEMA_FILE
SPECIFICATION_VERSION = "2.1.0"
RULESET_VERSION = "2.0.0"
REPORT_VERSION = "2.0.0"


class ContractViolation(ValueError):
    """A body does not satisfy the named definition of the frozen pack."""


def schema_path() -> Path:
    """The wheel ships a copy beside this module; a checkout reads the adopted pack directly."""
    return _PACKAGED if _PACKAGED.exists() else _REPOSITORY


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


_FORMATS = FormatChecker(formats=())
_FORMATS.checks("date-time")(_rfc3339)


@cache
def pack() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(schema_path().read_text(encoding="utf-8"))
    if not document.get("$id", "").endswith(f":{SPECIFICATION_VERSION}"):
        raise RuntimeError("the packaged wire schemas are not specification 2.1.0")
    Draft202012Validator.check_schema(document)
    return document


def definitions() -> list[str]:
    return sorted(pack()["$defs"])


@cache
def validator(definition: str) -> Draft202012Validator:
    document = pack()
    if definition not in document["$defs"]:
        raise KeyError(f"unknown Phase 4 definition {definition}")
    schema = {
        "$schema": document["$schema"],
        "$id": document["$id"],
        "$defs": document["$defs"],
        "$ref": f"#/$defs/{definition}",
    }
    return Draft202012Validator(schema, format_checker=_FORMATS)


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
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ContractViolation(f"body for {definition} is not UTF-8 JSON") from None
    validate(definition, body)
    return body
