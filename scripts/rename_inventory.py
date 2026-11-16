"""Engine-side evidence for the open worksheet-rename investigation (spec 3.0.0 §2).

Reports, for one workbook, what Team 9's production engine sees when it decides whether a
generic worksheet name can be renamed automatically (Phase 4 §7.3/§8.4):

* each WORKSHEET_NAME finding with its classification and finding ID;
* the workbook-wide rename blockers that force MANUAL_REVIEW;
* every reference site that names each generic sheet, by kind (cell formula, defined
  name, chart series, data validation, main-namespace conditional formula, Excel 2010
  x14 conditional formula, internal hyperlink location);
* the counterfactual classification under the stricter reading in which a known x14
  conditional formula counts as an "unsupported formula extension".

With --apply NAME it also runs the real correction, the engine's independent
preservation verifier and the output re-audit, and lists the rewritten references.

This is a diagnostic. It changes no rule, expectation, finding identity or fixture and
needs no network access. Workbook cell text is not printed; only formulas, names and
part paths are, so do not run it on confidential workbooks you cannot share.

    uv run python -m scripts.rename_inventory tests/fixtures/phase4/synthetic/rename_references.xlsx
    uv run python -m scripts.rename_inventory WORKBOOK --apply "Data source" --output out.json
"""

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.audit.engine import PHASE4_RULESET, OoxmlWorkbookEngine
from app.contracts.models import AuditRecord, AuditStatus, ProcessingLimits
from app.contracts.phase4 import Decision
from app.workbooks.formulas import scan_formula
from app.workbooks.inventory import WorkbookInventory, build_inventory

Site = dict[str, str]


def _names_sheet(text: str, sheet: str) -> bool:
    scan = scan_formula(text)
    return any(
        name.casefold() == sheet.casefold() for token in scan.sheet_tokens for name in token.names
    )


def reference_sites(inventory: WorkbookInventory, sheet: str) -> list[Site]:
    """Every reference-bearing location the engine reads that names `sheet`."""
    sites: list[Site] = []

    def add(kind: str, part: str, where: str, text: str) -> None:
        if _names_sheet(text, sheet):
            sites.append({"kind": kind, "part": part, "where": where, "formula": text})

    for model in inventory.sheets:
        worksheet = model.worksheet
        if worksheet is None:
            continue
        part = model.info.part
        for cell in sorted(worksheet.cells.values(), key=lambda item: (item.row, item.col)):
            if cell.formula is not None:
                add("cell_formula", part, cell.ref, cell.formula)
        for text in worksheet.validation_formulas:
            add("validation", part, "dataValidation", text)
        for conditional in worksheet.conditionals:
            for text in conditional.formulas:
                add("conditional_main", part, conditional.sqref, text)
        for text in worksheet.extension_formulas:
            add("conditional_x14", part, "x14:conditionalFormatting", text)
        for link in worksheet.hyperlinks:
            if link.location is not None:
                add("hyperlink_location", part, link.ref, link.location)
    for defined in inventory.defined_names:
        add("defined_name", "xl/workbook.xml", defined.name, defined.text)
    for chart, formulas in sorted(inventory.chart_formulas.items()):
        for index, text in enumerate(formulas):
            add("chart_formula", chart, str(index), text)
    return sites


def describe(data: bytes, filename: str) -> dict[str, Any]:
    engine = OoxmlWorkbookEngine()
    validated = engine.validate(data, filename, ProcessingLimits())
    evaluation = engine.audit(data, validated, PHASE4_RULESET)
    inventory = build_inventory(data, ProcessingLimits())
    x14_formulas = sum(
        len(model.worksheet.extension_formulas) for model in inventory.sheets if model.worksheet
    )
    opaque_extensions = any(
        model.worksheet.extension_formula_count or model.worksheet.extension_reference_unsupported
        for model in inventory.sheets
        if model.worksheet
    )
    findings = []
    for finding in evaluation.findings:
        if finding.ruleId != "WORKSHEET_NAME":
            continue
        sites = reference_sites(inventory, finding.worksheet)
        strict_manual = bool(inventory.rename_blockers) or x14_formulas > 0
        findings.append(
            {
                "worksheet": finding.worksheet,
                "target": finding.target,
                "findingId": finding.findingId,
                "classification": finding.classification.value,
                "referenceSites": sites,
                "referenceKinds": sorted({site["kind"] for site in sites}),
                # Rename safety is workbook-wide (§8.4 "complete reference-bearing inventory"):
                # any understood x14 formula anywhere would block every rename.
                "strictX14Classification": "MANUAL_REVIEW"
                if strict_manual
                else finding.classification.value,
            }
        )
    return {
        "sourceSha256": hashlib.sha256(data).hexdigest(),
        "rulesetVersion": PHASE4_RULESET,
        "sheets": [
            {"name": s.name, "part": s.part, "sheetId": s.sheet_id, "kind": s.kind}
            for s in inventory.structure.sheets
        ],
        "renameBlockers": list(inventory.rename_blockers),
        "x14ConditionalFormulasUnderstood": x14_formulas,
        "opaqueExtensionFormulasPresent": opaque_extensions,
        "worksheetNameFindings": findings,
        "limitations": list(evaluation.limitations),
    }


def apply_rename(
    data: bytes, filename: str, new_name: str, worksheet: str | None
) -> dict[str, Any]:
    engine = OoxmlWorkbookEngine()
    validated = engine.validate(data, filename, ProcessingLimits())
    evaluation = engine.audit(data, validated, PHASE4_RULESET)
    candidates = [
        f
        for f in evaluation.findings
        if f.ruleId == "WORKSHEET_NAME" and (worksheet is None or f.worksheet == worksheet)
    ]
    if len(candidates) != 1:
        raise SystemExit("Select exactly one WORKSHEET_NAME finding with --sheet.")
    finding = candidates[0]
    now = datetime.now(UTC)
    audit = AuditRecord(
        audit_id="0" * 32,
        workbook_id="1" * 32,
        version_id="2" * 32,
        owner_subject="diagnostic",
        session_id=None,
        ruleset_version=PHASE4_RULESET,
        status=AuditStatus.REMEDIATION_AVAILABLE,
        started_at=now,
        completed_at=now,
        findings=evaluation.findings,
        limitations=evaluation.limitations,
        error=None,
    )
    decisions = [Decision(findingId=finding.findingId, action="APPLY", value={"name": new_name})]
    result: dict[str, Any] = {"worksheet": finding.worksheet, "newName": new_name}
    try:
        output = engine.remediate(data, validated, audit, decisions)
        engine.verify_preservation(data, output, validated, audit, decisions)
        output_validated = engine.validate(output, filename, ProcessingLimits())
        output_evaluation = engine.audit(output, output_validated, PHASE4_RULESET)
        engine.verify_corrections(data, output, audit, decisions, output_evaluation)
    except Exception as exc:  # report the controlled refusal, e.g. 422 for a manual rename
        code = getattr(exc, "code", type(exc).__name__)
        result |= {"applied": False, "refusal": code, "message": str(exc)[:300]}
        return result
    output_inventory = build_inventory(output, ProcessingLimits())
    result |= {
        "applied": True,
        "outputSha256": hashlib.sha256(output).hexdigest(),
        "preservationVerified": True,
        "outputFindingsForSheet": [
            f.target for f in output_evaluation.findings if f.ruleId == "WORKSHEET_NAME"
        ],
        "rewrittenReferenceSites": reference_sites(output_inventory, new_name),
        "remainingOldNameSites": reference_sites(output_inventory, finding.worksheet),
    }
    result["_output_bytes"] = output
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--apply", metavar="NAME", help="rename the selected generic sheet")
    parser.add_argument("--sheet", help="generic sheet to rename when there are several")
    parser.add_argument("--output", type=Path, help="write the JSON report here")
    parser.add_argument("--save-output", type=Path, help="write the renamed workbook here")
    args = parser.parse_args(argv)
    data = args.workbook.read_bytes()
    report = describe(data, args.workbook.name)
    if args.apply:
        applied = apply_rename(data, args.workbook.name, args.apply, args.sheet)
        output = applied.pop("_output_bytes", None)
        if output is not None and args.save_output:
            args.save_output.write_bytes(output)
        report["apply"] = applied
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
