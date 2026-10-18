"""ALT_TEXT and WORKSHEET_NAME rules. Presence checks only; no content-quality inference."""

import re
from collections import Counter
from dataclasses import dataclass

from app.audit.identity import (
    cell_target,
    finding_id,
    object_target,
    worksheet_target,
    xml_target,
)
from app.contracts.models import Finding, FindingCategory, FindingClassification
from app.workbooks.drawings import DrawingInventory, DrawingObject, UnsupportedObject
from app.workbooks.structure import SheetInfo

ALT_TEXT = "ALT_TEXT"
WORKSHEET_NAME = "WORKSHEET_NAME"

#: Unicode White_Space, listed explicitly so trimming never depends on str.strip() rules.
WHITESPACE_CODE_POINTS = (
    *range(0x0009, 0x000D + 1),
    0x0020,
    0x0085,
    0x00A0,
    0x1680,
    *range(0x2000, 0x200A + 1),
    0x2028,
    0x2029,
    0x202F,
    0x205F,
    0x3000,
)
WHITESPACE = "".join(chr(code_point) for code_point in WHITESPACE_CODE_POINTS)
GENERIC_SHEET_NAME = re.compile(r"^sheet([0-9]+)$", re.IGNORECASE | re.ASCII)
_ID_PATTERN = re.compile(r"^[0-9]+$", re.ASCII)
_NAME_LIMIT = 80


def trim(value: str) -> str:
    return value.strip(WHITESPACE)


def is_blank(value: str | None) -> bool:
    return value is None or not trim(value)


def is_generic_worksheet_name(name: str) -> bool:
    match = GENERIC_SHEET_NAME.match(trim(name))
    return match is not None and int(match.group(1)) > 0


def _display(text: str) -> str:
    text = " ".join(trim(text).split())
    return text if len(text) <= _NAME_LIMIT else text[: _NAME_LIMIT - 1] + "\u2026"


@dataclass(frozen=True)
class RuleContext:
    source_sha256: str
    ruleset_version: str

    def finding(
        self,
        rule_id: str,
        category: FindingCategory,
        worksheet: str,
        target: str,
        classification: FindingClassification,
        message: str,
        recommendation: str,
    ) -> Finding:
        return Finding(
            findingId=finding_id(
                self.source_sha256, self.ruleset_version, rule_id, worksheet, target
            ),
            ruleId=rule_id,
            category=category,
            worksheet=worksheet,
            target=target,
            classification=classification,
            message=message,
            recommendation=recommendation,
        )


def worksheet_name_findings(context: RuleContext, sheet: SheetInfo) -> list[Finding]:
    if not is_generic_worksheet_name(sheet.name):
        return []
    return [
        context.finding(
            WORKSHEET_NAME,
            FindingCategory.WORKSHEET_NAME,
            sheet.name,
            worksheet_target(sheet.part, sheet.sheet_id),
            FindingClassification.REQUIRES_USER_INPUT,
            f'Worksheet "{_display(sheet.name)}" uses a generic default name.',
            "Rename the worksheet with a descriptive name that identifies its content.",
        )
    ]


def _object_targets(objects: list[DrawingObject]) -> tuple[list[str], list[str]]:
    """Targets per object (same order) plus limitations for unusual ID metadata."""
    usable = Counter(
        obj.object_id
        for obj in objects
        if obj.object_id is not None and _ID_PATTERN.match(obj.object_id)
    )
    targets: list[str] = []
    reasons: set[tuple[str, str]] = set()
    for obj in objects:
        object_id = obj.object_id
        if object_id is not None and _ID_PATTERN.match(object_id) and usable[object_id] == 1:
            targets.append(object_target(obj.part, object_id))
            continue
        if object_id is None or not object_id:
            reasons.add((obj.part, "missing"))
        elif not _ID_PATTERN.match(object_id):
            reasons.add((obj.part, "invalid"))
        else:
            reasons.add((obj.part, "duplicated"))
        targets.append(xml_target(obj.part, obj.cnvpr_path))
    limitations = [
        f"Drawing part {part} has {reason} object IDs; affected objects use XML element "
        "path targets (#xml=)."
        for part, reason in sorted(reasons)
    ]
    return targets, limitations


def _object_label(obj: DrawingObject) -> str:
    name = _display(obj.name)
    return f'{obj.kind} "{name}"' if name else obj.kind


def _object_finding(
    context: RuleContext, worksheet: str, obj: DrawingObject, target: str
) -> tuple[Finding | None, str | None]:
    label = _object_label(obj)
    if obj.decorative == "true":
        return (
            context.finding(
                ALT_TEXT,
                FindingCategory.NON_TEXT_CONTENT,
                worksheet,
                target,
                FindingClassification.MANUAL_REVIEW,
                f"{label} is marked decorative. A reviewer must confirm that it conveys no "
                "essential information.",
                "Confirm the object is purely decorative. If it conveys information, clear the "
                "decorative setting and provide an alternative text title and description.",
            ),
            None,
        )
    if obj.decorative == "ambiguous":
        return (
            context.finding(
                ALT_TEXT,
                FindingCategory.NON_TEXT_CONTENT,
                worksheet,
                target,
                FindingClassification.MANUAL_REVIEW,
                f"{label} has unrecognized decorative metadata that requires manual review.",
                "Review whether the object is decorative. Set the decorative option explicitly, "
                "or provide an alternative text title and description.",
            ),
            f"Decorative metadata for {target} has an unrecognized value; reviewed manually.",
        )
    missing_title = is_blank(obj.title)
    missing_descr = is_blank(obj.descr)
    if not missing_title and not missing_descr:
        return None, None
    if missing_title and missing_descr:
        what = "title and description"
        recommendation = "Provide an alternative text title and a short description."
    elif missing_title:
        what = "title"
        recommendation = "Provide an alternative text title."
    else:
        what = "description"
        recommendation = "Provide a short alternative text description."
    return (
        context.finding(
            ALT_TEXT,
            FindingCategory.NON_TEXT_CONTENT,
            worksheet,
            target,
            FindingClassification.REQUIRES_USER_INPUT,
            f"{label} is missing its alternative text {what}.",
            recommendation + " If the object is purely decorative, mark it as decorative.",
        ),
        None,
    )


def _unsupported_finding(context: RuleContext, worksheet: str, item: UnsupportedObject) -> Finding:
    if item.cell_ref is not None:
        target = cell_target(item.part, item.cell_ref)
    elif item.element_path:
        target = xml_target(item.part, item.element_path)
    else:
        target = item.part
    return context.finding(
        ALT_TEXT,
        FindingCategory.NON_TEXT_CONTENT,
        worksheet,
        target,
        FindingClassification.MANUAL_REVIEW,
        f"{item.label} could not be inspected automatically for alternative text.",
        "Review this object manually and make sure meaningful content has alternative text.",
    )


def alt_text_findings(
    context: RuleContext, worksheet: str, inventory: DrawingInventory
) -> tuple[list[Finding], list[str]]:
    """One finding per object at most. Decorative objects never get a missing-fields finding."""
    findings: list[Finding] = []
    limitations = list(inventory.limitations)
    targets, id_limitations = _object_targets(inventory.objects)
    limitations.extend(id_limitations)
    for obj, target in zip(inventory.objects, targets, strict=True):
        finding, limitation = _object_finding(context, worksheet, obj, target)
        if finding is not None:
            findings.append(finding)
        if limitation is not None:
            limitations.append(limitation)
    for item in inventory.unsupported:
        findings.append(_unsupported_finding(context, worksheet, item))
        limitations.append(item.limitation)
    return findings, limitations
