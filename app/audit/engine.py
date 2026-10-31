"""WorkbookEngine adapter: register `app.audit.engine.OoxmlWorkbookEngine` once at assembly.

Pure computation over immutable bytes: no HTTP, sessions, storage, network, AWS, logging
of workbook content, file writes or workbook saves. Ruleset 1.0.0 keeps the Phase 3
two-rule behavior for regression and historical audits; ruleset 2.0.0 runs all six
rules with review context and implements the CorrectionEngine methods.
"""

import hashlib
from collections.abc import Mapping

from pydantic import TypeAdapter

from app.audit.identity import finding_id, finding_sort_key
from app.audit.objects import package_inventory, sheet_inventories
from app.audit.review import Correction, Entry, review_item
from app.audit.rules import RuleContext, alt_text_findings, worksheet_name_findings
from app.audit.rules_v2 import ambiguous_label, analyze
from app.contracts.errors import ProjectException
from app.contracts.models import (
    AuditEvaluation,
    AuditRecord,
    Finding,
    FindingClassification,
    ProcessingLimits,
    SemVer,
    ValidatedWorkbook,
)
from app.contracts.phase4 import RULE_IDS, Decision
from app.workbooks.editor import EditFailed, PlannedEdit, apply_edits, plan_new_tables
from app.workbooks.inventory import WorkbookInventory, build_inventory
from app.workbooks.preservation import PreservationError, verify_preservation
from app.workbooks.validation import inspect_package, validate_workbook
from app.workbooks.xmlsafe import local_name

_SEMVER: TypeAdapter[str] = TypeAdapter(SemVer)
#: Sheet kinds the rules inspect; dialogsheets are outside the rules.
AUDITED_SHEET_KINDS = frozenset({"worksheet", "chartsheet"})
#: Worksheet value for package-level findings (pod contract 1.1 §8 workbook-level location).
WORKBOOK_LEVEL = "__WORKBOOK__"
LEGACY_RULESET = "1.0.0"
PHASE4_RULESET = "2.0.0"
FAMILY_ORDER = {"alt": 0, "link": 1, "header_new": 2, "header_enable": 2, "rename": 3}


class AuditInputError(ProjectException):
    """The audit input does not match its validation result (worker records a failure)."""


def _unsupported(
    message: str = "This finding cannot be corrected automatically.",
) -> ProjectException:
    return ProjectException(422, "UNSUPPORTED_CORRECTION", message)


def _invalid(message: str) -> ProjectException:
    return ProjectException(422, "INVALID_CORRECTION", message)


class OoxmlWorkbookEngine:
    """Implements app.contracts.ports.CorrectionEngine (and WorkbookEngine)."""

    supported_rulesets: tuple[str, ...] = (LEGACY_RULESET, PHASE4_RULESET)

    def __init__(self, audit_limits: ProcessingLimits | None = None) -> None:
        # Package/XML bounds applied when re-reading during audit. Worksheet and cell
        # limits are validation-time decisions and are not re-enforced here.
        self._audit_limits = audit_limits or ProcessingLimits()

    def validate(self, data: bytes, filename: str, limits: ProcessingLimits) -> ValidatedWorkbook:
        return validate_workbook(data, filename, limits)

    # -- audit -----------------------------------------------------------------------

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
        if ruleset_version == PHASE4_RULESET:
            return self._audit_phase4(data, validated, source_sha256)
        # Phase 3 behavior: other ruleset labels only feed finding identity. The backend
        # decides which ruleset is installed; this engine advertises supported_rulesets.
        return self._audit_legacy(data, validated, source_sha256, ruleset_version)

    def _audit_legacy(
        self, data: bytes, validated: ValidatedWorkbook, source_sha256: str, ruleset_version: str
    ) -> AuditEvaluation:
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
            for inventory in sheet_inventories(package, sheet)
        ]
        inventories.append((WORKBOOK_LEVEL, package_inventory(package)))
        for sheet in structure.sheets:
            if sheet.kind in AUDITED_SHEET_KINDS:
                findings.extend(worksheet_name_findings(context, sheet))
        for worksheet, inventory in inventories:
            sheet_findings, sheet_limitations = alt_text_findings(context, worksheet, inventory)
            findings.extend(sheet_findings)
            limitations.update(sheet_limitations)
        findings.sort(key=finding_sort_key)
        return AuditEvaluation(findings=findings, limitations=sorted(limitations))

    def _analyze(
        self, data: bytes, source_sha256: str, ruleset_version: str
    ) -> tuple[WorkbookInventory, list[Entry], list[str]]:
        inventory = build_inventory(data, self._audit_limits)
        entries, limitations = analyze(RuleContext(source_sha256, ruleset_version), inventory)
        return inventory, entries, limitations

    def _audit_phase4(
        self, data: bytes, validated: ValidatedWorkbook, source_sha256: str
    ) -> AuditEvaluation:
        inventory, entries, limitations = self._analyze(data, source_sha256, PHASE4_RULESET)
        if inventory.structure.worksheet_parts != validated.worksheet_parts:
            raise AuditInputError(
                500,
                "AUDIT_INPUT_MISMATCH",
                "The workbook structure does not match its validation result.",
            )
        return AuditEvaluation(
            findings=[entry.finding for entry in entries],
            limitations=limitations,
            review_context=[review_item(entry) for entry in entries],
            checks_performed=list(RULE_IDS),
        )

    # -- corrections -----------------------------------------------------------------

    def _plan(
        self, data: bytes, audit: AuditRecord, decisions: list[Decision]
    ) -> tuple[WorkbookInventory, list[PlannedEdit]]:
        """Resolve APPLY decisions against the immutable source inventory.

        Raises 422 UNSUPPORTED_CORRECTION / INVALID_CORRECTION / CONFLICTING_CORRECTIONS
        for requests outside the safe profile, examining decisions in input order.
        """
        source_sha256 = hashlib.sha256(data).hexdigest()
        if audit.ruleset_version != PHASE4_RULESET:
            raise _unsupported("Corrections require a ruleset 2.0.0 audit.")
        for finding in audit.findings:
            expected = finding_id(
                source_sha256,
                audit.ruleset_version,
                finding.ruleId,
                finding.worksheet,
                finding.target,
            )
            if expected != finding.findingId:
                raise ProjectException(
                    500,
                    "SOURCE_INTEGRITY_FAILED",
                    "The audit does not describe these workbook bytes.",
                )
        inventory, entries, _ = self._analyze(data, source_sha256, audit.ruleset_version)
        by_id = {entry.finding.findingId: entry for entry in entries}
        positions = {finding.findingId: index for index, finding in enumerate(audit.findings)}
        planned: list[tuple[int, int, PlannedEdit]] = []
        link_cells: dict[tuple[str, tuple[int, int]], str] = {}
        header_rows: list[tuple[str, int, int, int]] = []
        for decision in decisions:
            if decision.findingId not in positions:
                raise ProjectException(
                    404, "FINDING_NOT_FOUND", "The source audit has no such finding."
                )
            if decision.action != "APPLY":
                continue
            entry = by_id.get(decision.findingId)
            if (
                entry is None
                or entry.correction is None
                or entry.finding.classification != FindingClassification.REQUIRES_USER_INPUT
            ):
                raise _unsupported()
            value = decision.value
            if not isinstance(value, dict):
                raise _invalid("The correction value must be an object.")
            correction = entry.correction
            self._check_value(correction, entry, value)
            if correction.kind == "link" and correction.cell is not None:
                link_cells[(correction.part, correction.cell)] = decision.findingId
            if correction.kind in ("header_new", "header_enable") and correction.rect is not None:
                sheet_part = correction.part
                if correction.kind == "header_enable":
                    table = next(t for t in inventory.all_tables if t.part == correction.part)
                    sheet_part = table.sheet_part
                rect = correction.rect
                header_rows.append((sheet_part, rect.top, rect.left, rect.right))
            planned.append(
                (
                    FAMILY_ORDER[correction.kind],
                    positions[decision.findingId],
                    PlannedEdit(correction, value),
                )
            )
        for (part, (row, col)), _ in link_cells.items():
            for sheet_part, top, left, right in header_rows:
                if part == sheet_part and row == top and left <= col <= right:
                    raise ProjectException(
                        422,
                        "CONFLICTING_CORRECTIONS",
                        "A link label change conflicts with a header row being confirmed.",
                    )
        planned.sort(key=lambda item: (item[0], item[1]))
        return inventory, [item[2] for item in planned]

    @staticmethod
    def _check_value(correction: Correction, entry: Entry, value: Mapping[str, object]) -> None:
        current = entry.current or {}
        if correction.kind == "alt":
            if set(value) != {"title", "description"} or not all(
                isinstance(item, str) for item in value.values()
            ):
                raise _invalid("Alternative text needs a title and a description.")
            if value == current:
                raise _invalid("The correction makes no effective change.")
        elif correction.kind == "link":
            text = value.get("text")
            if set(value) != {"text"} or not isinstance(text, str):
                raise _invalid("Link text must be a string.")
            if text == current.get("text") or ambiguous_label(text, None):
                raise _invalid("The replacement hyperlink label remains ambiguous.")
        elif correction.kind in ("header_new", "header_enable"):
            row = value.get("headerRow")
            if set(value) != {"headerRow"} or type(row) is not int:
                raise _invalid("Confirm the header with its row number.")
            if correction.rect is None or row != correction.rect.top:
                raise _invalid("Confirm only the candidate's first row.")
        elif correction.kind == "rename":
            name = value.get("name")
            if set(value) != {"name"} or not isinstance(name, str) or name == correction.sheet_name:
                raise _invalid("The worksheet name is invalid.")

    def validate_decisions(
        self,
        data: bytes,
        validated: ValidatedWorkbook,
        audit: AuditRecord,
        decisions: list[Decision],
    ) -> None:
        self._plan(data, audit, decisions)

    def remediate(
        self,
        data: bytes,
        validated: ValidatedWorkbook,
        audit: AuditRecord,
        decisions: list[Decision],
    ) -> bytes:
        inventory, edits = self._plan(data, audit, decisions)
        if not edits:
            return data
        try:
            return apply_edits(data, inventory, edits)
        except EditFailed as exc:
            raise ProjectException(
                500,
                "OUTPUT_VALIDATION_FAILED",
                "An approved correction could not be applied safely.",
            ) from exc

    def verify_preservation(
        self,
        data: bytes,
        candidate: bytes,
        validated: ValidatedWorkbook,
        audit: AuditRecord,
        decisions: list[Decision],
    ) -> None:
        inventory, edits = self._plan(data, audit, decisions)
        if not edits:
            if candidate != data:
                raise ProjectException(
                    500, "PRESERVATION_FAILED", "An unchanged candidate differs from its source."
                )
            return
        try:
            verify_preservation(data, candidate, inventory, edits)
        except PreservationError as exc:
            raise ProjectException(
                500,
                "PRESERVATION_FAILED",
                "An unapproved change was detected in the candidate workbook.",
            ) from exc

    def verify_corrections(
        self,
        data: bytes,
        candidate: bytes,
        audit: AuditRecord,
        decisions: list[Decision],
        evaluation: AuditEvaluation,
    ) -> None:
        inventory, edits = self._plan(data, audit, decisions)
        if not edits:
            return
        output = build_inventory(candidate, self._audit_limits)
        remaining = {(finding.ruleId, finding.target) for finding in evaluation.findings}
        tables = plan_new_tables(
            inventory, [edit.correction for edit in edits if edit.correction.kind == "header_new"]
        )
        new_tables = {table.correction: table for table in tables}
        for edit in edits:
            if not self._effective(edit, inventory, output, remaining, new_tables):
                raise ProjectException(
                    500,
                    "OUTPUT_AUDIT_FAILED",
                    "An approved correction is not present in the output workbook.",
                )

    @staticmethod
    def _effective(
        edit: PlannedEdit,
        source: WorkbookInventory,
        output: WorkbookInventory,
        remaining: set[tuple[str, str]],
        new_tables: Mapping[Correction, object],
    ) -> bool:
        correction, value = edit.correction, edit.value
        if correction.kind == "alt":
            target = f"{correction.part}#id={correction.object_id}"
            stored = output.package.find(correction.part)
            if stored is None or ("ALT_TEXT", target) in remaining:
                return False
            matches = [
                node
                for node in output.package.parse_xml(stored).iter()
                if local_name(node.tag) == "cNvPr" and node.get("id") == correction.object_id
            ]
            return (
                len(matches) == 1
                and matches[0].get("title") == value["title"]
                and matches[0].get("descr") == value["description"]
            )
        if correction.kind == "link":
            assert correction.cell is not None
            model = next((m for m in output.sheets if m.info.part == correction.part), None)
            if model is None or model.worksheet is None:
                return False
            cell = model.worksheet.cells.get(correction.cell)
            target = f"{correction.part}#cell={cell.ref if cell else ''}"
            return (
                cell is not None
                and cell.cell_type == "inlineStr"
                and cell.text == value["text"]
                and ("LINK_TEXT", target) not in remaining
            )
        if correction.kind == "header_new":
            model = next((m for m in output.sheets if m.info.part == correction.part), None)
            planned = new_tables.get(correction)
            if model is None or planned is None or correction.rect is None:
                return False
            range_target = f"{correction.part}#range={correction.rect.canonical()}"
            tables = [t for t in model.tables if t.rect == correction.rect and t.header_rows == 1]
            return (
                len(tables) == 1
                and ("TABLE_HEADER", range_target) not in remaining
                and not any(
                    rule == "TABLE_HEADER" and target.startswith(tables[0].part + "#")
                    for rule, target in remaining
                )
            )
        if correction.kind == "header_enable":
            tables = [t for t in output.all_tables if t.part == correction.part]
            return (
                len(tables) == 1
                and tables[0].header_rows == 1
                and not any(
                    rule == "TABLE_HEADER" and target.startswith(correction.part + "#")
                    for rule, target in remaining
                )
            )
        if correction.kind == "rename":
            position = (correction.sheet_position or 0) - 1
            before = source.structure.sheets[position]
            after = (
                output.structure.sheets[position]
                if 0 <= position < len(output.structure.sheets)
                else None
            )
            target = f"{before.part}#sheetId={before.sheet_id}"
            return (
                after is not None
                and after.name == value["name"]
                and after.part == before.part
                and after.sheet_id == before.sheet_id
                and ("WORKSHEET_NAME", target) not in remaining
            )
        return False
