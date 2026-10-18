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
    inventory_drawing,
    inventory_vml,
)
from app.workbooks.package import OoxmlPackage, Relationship, rel_type_is
from app.workbooks.structure import SheetInfo, SheetReference
from app.workbooks.validation import inspect_package, validate_workbook

_SEMVER: TypeAdapter[str] = TypeAdapter(SemVer)
#: Sheet kinds the rules inspect; dialogsheets are outside the Phase 3 rules.
AUDITED_SHEET_KINDS = frozenset({"worksheet", "chartsheet"})
#: Worksheet value for package-level findings (pod contract 1.1 §8 workbook-level location).
WORKBOOK_LEVEL = "__WORKBOOK__"


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
        inventories = [
            (sheet.name, inventory)
            for sheet in structure.sheets
            if sheet.kind in AUDITED_SHEET_KINDS
            for inventory in _sheet_inventories(package, sheet)
        ]
        inventories.append((WORKBOOK_LEVEL, _package_inventory(package)))
        for sheet in structure.sheets:
            if sheet.kind in AUDITED_SHEET_KINDS:
                findings.extend(worksheet_name_findings(context, sheet))
        for worksheet, inventory in inventories:
            sheet_findings, sheet_limitations = alt_text_findings(context, worksheet, inventory)
            findings.extend(sheet_findings)
            limitations.update(sheet_limitations)
        findings.sort(key=finding_sort_key)
        return AuditEvaluation(findings=findings, limitations=sorted(limitations))


def _sheet_inventories(package: OoxmlPackage, sheet: SheetInfo) -> list[DrawingInventory]:
    rels = package.relationships(sheet.part)
    inventories: list[DrawingInventory] = []
    for ref in sheet.references:
        rel = rels.get(ref.rel_id or "")
        resolved = rel.resolved if rel is not None and not rel.external else None
        if ref.element == "drawing":
            if resolved is not None and rel is not None and rel_type_is(rel.rel_type, "drawing"):
                inventories.append(inventory_drawing(package, resolved))
            else:
                inventories.append(_review_reference(sheet, ref, rel))
            continue
        if resolved is not None and resolved.casefold().endswith(".vml"):
            inventory = inventory_vml(package, resolved)
            if inventory.objects or inventory.unsupported:
                inventories.append(inventory)
                continue
        inventories.append(_review_reference(sheet, ref, rel))
    if sheet.rich_value_cells or sheet.rich_value_cells_omitted:
        inventories.append(_cell_image_inventory(sheet))
    return inventories


def _cell_image_inventory(sheet: SheetInfo) -> DrawingInventory:
    """Cells holding an in-cell picture, an IMAGE() result or another rich value."""
    limitation = (
        f"Worksheet part {sheet.part} has cell images or rich values; their contents were "
        "not inspected."
    )
    unsupported = [
        UnsupportedObject(
            sheet.part,
            cell.path,
            f"In-cell picture or rich value{f' in {cell.ref}' if cell.ref else ''}",
            limitation,
        )
        for cell in sheet.rich_value_cells
    ]
    extra = []
    if sheet.rich_value_cells_omitted:
        extra.append(
            f"Worksheet part {sheet.part} has {sheet.rich_value_cells_omitted} more cell "
            "image(s) beyond the listed limit; review them manually."
        )
    return DrawingInventory([], unsupported, extra)


def _package_inventory(package: OoxmlPackage) -> DrawingInventory:
    """Cell-image and rich-data parts, reported at workbook level (pod contract 1.1 §8)."""
    unsupported = [
        UnsupportedObject(
            name,
            (),
            "Cell-image or rich-data part",
            f"Package part {name}: cell-image/rich-data representations are not assessed.",
            part_root=True,
        )
        for name in package.entry_names
        if name.startswith("xl/richData/") or name == "xl/cellimages.xml"
    ]
    return DrawingInventory([], unsupported, [])


REFERENCE_LABELS = {
    "drawing": "Drawing reference",
    "legacyDrawing": "Legacy drawing reference",
    "legacyDrawingHF": "Header/footer drawing reference",
    "picture": "Worksheet background image",
    "control": "Form control",
    "oleObject": "Embedded OLE object",
}


def _review_reference(
    sheet: SheetInfo, ref: SheetReference, rel: Relationship | None
) -> DrawingInventory:
    label = REFERENCE_LABELS[ref.element]
    if rel is None or rel.resolved is None and not rel.external:
        detail = "its relationship does not resolve to a part"
    elif rel.external:
        detail = "external content is not fetched"
    else:
        detail = "it is not automatically assessed"
    return DrawingInventory(
        [],
        [UnsupportedObject(sheet.part, ref.path, label, f"{label} in {sheet.part}: {detail}.")],
        [],
    )
