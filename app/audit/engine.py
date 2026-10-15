"""WorkbookEngine adapter: register `app.audit.engine.OoxmlWorkbookEngine` once at assembly.

Pure computation over immutable bytes: no HTTP, sessions, storage, network, AWS, logging
of workbook content, file writes or workbook saves.
"""

import hashlib

from pydantic import TypeAdapter

from app.audit.identity import finding_sort_key
from app.audit.rules import RuleContext, alt_text_findings, worksheet_name_findings
from app.contracts.errors import ProjectException
from app.contracts.models import (
    AuditEvaluation,
    Finding,
    ProcessingLimits,
    SemVer,
    ValidatedWorkbook,
)
from app.workbooks.drawings import (
    DrawingInventory,
    UnsupportedObject,
    XmlPath,
    inventory_drawing,
    inventory_vml,
)
from app.workbooks.package import OoxmlPackage, Relationship
from app.workbooks.structure import SheetInfo, SheetReference
from app.workbooks.validation import inspect_package, validate_workbook

_SEMVER: TypeAdapter[str] = TypeAdapter(SemVer)


class AuditInputError(ProjectException):
    """The audit input does not match its validation result (worker records a failure)."""


class OoxmlWorkbookEngine:
    """Implements app.contracts.ports.WorkbookEngine."""

    def __init__(self, audit_limits: ProcessingLimits | None = None) -> None:
        # Package/XML bounds applied when re-reading during audit. Worksheet and cell
        # limits are validation-time decisions and are not re-enforced here.
        self._audit_limits = audit_limits or ProcessingLimits()

    def validate(self, data: bytes, filename: str, limits: ProcessingLimits) -> ValidatedWorkbook:
        return validate_workbook(data, filename, limits)

    def audit(
        self, data: bytes, validated: ValidatedWorkbook, ruleset_version: str
    ) -> AuditEvaluation:
        _SEMVER.validate_python(ruleset_version)
        source_sha256 = hashlib.sha256(data).hexdigest()
        if source_sha256 != validated.source_sha256:
            raise AuditInputError(
                500,
                "SOURCE_INTEGRITY_FAILED",
                "The workbook bytes do not match the validated source hash.",
            )
        package, structure = inspect_package(data, self._audit_limits, enforce_content_limits=False)
        if structure.worksheet_parts != validated.worksheet_parts:
            raise AuditInputError(
                500,
                "AUDIT_INPUT_MISMATCH",
                "The workbook structure does not match its validation result.",
            )
        context = RuleContext(source_sha256, ruleset_version)
        findings: list[Finding] = []
        limitations: set[str] = set()
        for sheet in structure.sheets:
            findings.extend(worksheet_name_findings(context, sheet))
            for inventory in _sheet_inventories(package, sheet):
                sheet_findings, sheet_limitations = alt_text_findings(
                    context, sheet.name, inventory
                )
                findings.extend(sheet_findings)
                limitations.update(sheet_limitations)
        findings.sort(key=finding_sort_key)
        return AuditEvaluation(findings=findings, limitations=sorted(limitations))


def _sheet_inventories(package: OoxmlPackage, sheet: SheetInfo) -> list[DrawingInventory]:
    rels = package.relationships(sheet.part)
    inventories: list[DrawingInventory] = []
    for ref in sheet.references:
        path: XmlPath = ((sheet.root_name, 1), (ref.element, ref.index))
        if ref.element in ("oleObjects", "controls"):
            inventories.append(
                DrawingInventory(
                    [],
                    [],
                    [
                        f"Worksheet part {sheet.part} contains {ref.element}; their visible "
                        "representations are only reviewed through legacy VML findings."
                    ],
                )
            )
            continue
        if ref.element == "picture":
            inventories.append(
                _unsupported(
                    sheet.part,
                    path,
                    "Worksheet background image",
                    "background images cannot carry alt text and need manual review.",
                )
            )
            continue
        rel = rels.get(ref.rel_id or "")
        if rel is None or rel.external or rel.resolved is None:
            inventories.append(_unresolved(sheet, ref, path, rel))
        elif ref.element == "drawing":
            inventories.append(inventory_drawing(package, rel.resolved))
        else:
            inventories.append(inventory_vml(package, rel.resolved))
    return inventories


def _unsupported(part: str, path: XmlPath, label: str, detail: str) -> DrawingInventory:
    return DrawingInventory(
        [], [UnsupportedObject(part, path, label, f"{label} in {part}: {detail}")], []
    )


def _unresolved(
    sheet: SheetInfo, ref: SheetReference, path: XmlPath, rel: Relationship | None
) -> DrawingInventory:
    reason = "is external" if rel is not None and rel.external else "does not resolve to a part"
    return _unsupported(
        sheet.part,
        path,
        f"Unresolved {ref.element} reference",
        f"the relationship {reason}; its objects were not inspected.",
    )
