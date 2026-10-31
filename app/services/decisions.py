"""Backend decision validation; workbook interpretation stays behind the engine port."""

import json
import re
import unicodedata

from pydantic import ValidationError

from app.contracts.errors import ProjectException
from app.contracts.models import AuditRecord, ContractModel, ValidatedWorkbook
from app.contracts.phase4 import (
    AltTextValue,
    Decision,
    HeaderRowValue,
    LinkTextValue,
    RemediationRequest,
    ReviewItem,
    WorksheetNameValue,
)

# Unicode White_Space, explicitly frozen by specification 2.1.0 (not str.strip's superset).
WHITE_SPACE = (
    "\t\n\v\f\r \u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)
SPACE_PATTERN = re.compile(f"[{WHITE_SPACE}]+")
GENERIC_NAME = re.compile(r"sheet([0-9]+)", re.IGNORECASE | re.ASCII)
AMBIGUOUS_LABELS = {
    "click here",
    "here",
    "link",
    "more",
    "read more",
    "learn more",
    "website",
    "url",
}


def parse_remediation_request(raw: object) -> RemediationRequest:
    """Parse envelope only; replacement shapes belong to semantic 422 validation."""
    try:
        return RemediationRequest.model_validate(raw)
    except ValidationError as exc:
        raise ProjectException(
            400, "INVALID_REQUEST", "Decision request fields are invalid."
        ) from exc


def _invalid(message: str = "The correction value is invalid.") -> ProjectException:
    return ProjectException(422, "INVALID_CORRECTION", message)


def _generic(name: str) -> bool:
    match = GENERIC_NAME.fullmatch(name.strip(WHITE_SPACE))
    return match is not None and any(character != "0" for character in match.group(1))


def _xml_text(value: str) -> bool:
    return all(
        character in "\t\n\r"
        or 0x20 <= ord(character) <= 0xD7FF
        or 0xE000 <= ord(character) <= 0xFFFD
        or 0x10000 <= ord(character) <= 0x10FFFF
        for character in value
    )


def _ambiguous(text: str) -> bool:
    normalized = SPACE_PATTERN.sub(" ", text.strip(WHITE_SPACE)).casefold()
    if normalized[-1:] in (".", "!", "?"):
        normalized = normalized[:-1]
    return normalized in AMBIGUOUS_LABELS or normalized.startswith(
        ("http://", "https://", "www.", "mailto:")
    )


def validate_decisions(
    request: RemediationRequest,
    audit: AuditRecord,
    review: list[ReviewItem],
    validated: ValidatedWorkbook,
) -> list[Decision]:
    """Validate typed values and source-name interactions without editing workbook bytes.

    The caller authorizes resources, checks active/latest/terminal state, and invokes
    engine package preflight separately. The returned order matches submitted order;
    storage/report/engine ordering is derived from the immutable source audit.
    """
    findings = {finding.findingId: finding for finding in audit.findings}
    contexts = {item.findingId: item for item in review}
    names = {name.casefold(): name for name in validated.worksheet_parts}
    final_names: set[str] = set()
    seen: set[str] = set()
    for decision in request.decisions:
        if decision.findingId in seen:
            raise ProjectException(400, "DUPLICATE_DECISION", "A finding has multiple decisions.")
        seen.add(decision.findingId)
        finding = findings.get(decision.findingId)
        if finding is None:
            raise ProjectException(
                404, "FINDING_NOT_FOUND", "The source audit has no such finding."
            )
        supplied_value = "value" in decision.model_fields_set
        if decision.action != "APPLY":
            if supplied_value:
                raise ProjectException(400, "INVALID_REQUEST", "This action must omit value.")
            continue
        if not supplied_value:
            raise ProjectException(400, "MISSING_VALUE", "APPLY requires a value object.")
        item = contexts.get(decision.findingId)
        if (
            finding.classification != "REQUIRES_USER_INPUT"
            or finding.ruleId not in {"ALT_TEXT", "WORKSHEET_NAME", "LINK_TEXT", "TABLE_HEADER"}
            or item is None
            or "APPLY" not in item.allowedActions
        ):
            raise ProjectException(
                422, "UNSUPPORTED_CORRECTION", "This finding requires manual review."
            )
        value_models: dict[str, type[ContractModel]] = {
            "ALT_TEXT": AltTextValue,
            "WORKSHEET_NAME": WorksheetNameValue,
            "LINK_TEXT": LinkTextValue,
            "TABLE_HEADER": HeaderRowValue,
        }
        model = value_models[finding.ruleId]
        try:
            typed = model.model_validate(decision.value)
        except ValidationError as exc:
            raise _invalid() from exc
        values = typed.model_dump()
        for value in values.values():
            if isinstance(value, str) and (not value.strip(WHITE_SPACE) or not _xml_text(value)):
                raise _invalid("Correction text must be nonblank legal XML text.")
        if (
            len(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            > 8192
        ):
            raise _invalid("The correction value exceeds its byte limit.")
        if finding.ruleId == "WORKSHEET_NAME":
            name = values["name"]
            if name.casefold() == finding.worksheet.casefold() and name != finding.worksheet:
                raise ProjectException(
                    422, "UNSUPPORTED_CORRECTION", "Case-only rename is unsupported."
                )
            if (
                name != name.strip(WHITE_SPACE)
                or name == finding.worksheet
                or name.casefold() == "history"
                or _generic(name)
                or any(
                    character in ":\\/?*[]" or unicodedata.category(character) == "Cc"
                    for character in name
                )
                or name.startswith("'")
                or name.endswith("'")
            ):
                raise _invalid("The worksheet name is invalid.")
            folded = name.casefold()
            if folded in names or folded in final_names:
                raise ProjectException(
                    422,
                    "CONFLICTING_CORRECTIONS",
                    "Worksheet names must remain unique and cannot reuse source names.",
                )
            final_names.add(folded)
        elif finding.ruleId == "LINK_TEXT" and _ambiguous(values["text"]):
            raise _invalid("The replacement hyperlink label remains ambiguous.")
        elif finding.ruleId == "TABLE_HEADER":
            if item.currentValue is None or values["headerRow"] != item.currentValue.get(
                "headerRow"
            ):
                raise _invalid("Confirm only the candidate's first row.")
        elif finding.ruleId == "ALT_TEXT" and item.currentValue == values:
            raise _invalid("The correction makes no effective change.")
    return list(request.decisions)
