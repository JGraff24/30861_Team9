"""Build (or --check) the runnable Phase 4 manifest from the normative 2.1.0 test plan.

`docs/contracts/phase4/phase4-test-plan.json` is an assertion inventory, not a runnable suite
(§15.3). This script binds every hosted entry to one check handler with explicit parameters,
keeps the plan's IDs, order, components, acceptance criteria, operations and prerequisites, and
writes `tests/fixtures/manifest-phase4.json`. Local-integration and manual-release entries stay
outside the hosted report (§16); they are tracked in the run record and the implementation notes.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from tester.phase4.catalog import variants

REPO = Path(__file__).resolve().parents[1]
PLAN = REPO / "docs" / "contracts" / "phase4" / "phase4-test-plan.json"
PHASE3_MANIFEST = REPO / "tests" / "fixtures" / "manifest.json"
OUTPUT = REPO / "tests" / "fixtures" / "manifest-phase4.json"
MANIFEST_VERSION = "2.1.1"

LIMITS = {
    "suite_seconds": 1800,
    "run_read_timeout_seconds": 1830,
    "audit_poll_seconds": 80,
    "remediation_poll_seconds": 210,
    "business_get_seconds": 10,
    "upload_seconds": 35,
    "download_seconds": 30,
    "system_setup_seconds": 30,
    "json_response_bytes": 67_108_864,
    "binary_response_bytes": 33_554_432,
}

# Spec §5 delivered paths; operations 11-14 remain Phase 5 and must return the project 404.
PHASE4_OPERATIONS: dict[int | str, tuple[str, str, str]] = {
    1: ("GET", "/api/v1/health", "Check service availability"),
    2: ("POST", "/api/v1/workbooks", "Upload an initial workbook"),
    3: ("POST", "/api/v1/workbooks/{workbookId}/versions", "Upload a new workbook version"),
    4: ("GET", "/api/v1/workbooks/{workbookId}/versions/{versionId}", "Retrieve a version"),
    5: (
        "GET",
        "/api/v1/workbooks/{workbookId}/versions/{versionId}/audits/{auditId}",
        "Retrieve an audit and its findings",
    ),
    6: (
        "POST",
        "/api/v1/workbooks/{workbookId}/versions/{versionId}/audits",
        "Trigger a manual re-audit",
    ),
    7: (
        "POST",
        "/api/v1/workbooks/{workbookId}/versions/{versionId}/remediations",
        "Submit remediation decisions",
    ),
    8: ("GET", "/api/v1/remediations/{remediationId}", "Retrieve remediation status"),
    9: ("GET", "/api/v1/outputs/{outputId}/workbook", "Download the remediated workbook"),
    10: ("GET", "/api/v1/workbooks/{workbookId}/history", "Retrieve version and audit history"),
    "S": (
        "GET",
        "/api/v1/workbooks/{workbookId}/versions/{versionId}/source",
        "Download a version's source workbook",
    ),
    "V": (
        "GET",
        "/api/v1/workbooks/{workbookId}/versions/{versionId}/audits/{auditId}/review",
        "Retrieve review context",
    ),
    "R": (
        "GET",
        "/api/v1/workbooks/{workbookId}/versions/{versionId}/audits/{auditId}/report",
        "Download the JSON audit report",
    ),
}
ADDED_REQUIREMENTS = {
    "S": ["REQ-03", "REQ-08"],
    "V": ["REQ-06", "REQ-07"],
    "R": ["REQ-08", "REQ-10"],
}

COMPONENTS = {
    "C1": "Authentication and access",
    "C2": "Intake and validation",
    "C3": "Immutable sources and version metadata",
    "C4": "Six-rule audit engine",
    "C5": "Durable jobs and audit API",
    "C6": "Isolated sessions and actual execution measurement",
    "C7": "Human decisions and remediation",
    "C8": "Downloads reports and history",
    "C9": "Review user interface",
}

ACCESS = re.compile(
    r"P4-ACCESS-(NEW-VERSION|REAUDIT|DECISIONS|REMEDIATION|OUTPUT|HISTORY|SOURCE|REPORT|REVIEW)-"
    r"(UNAUTHENTICATED|INVALID-TOKEN|FOREIGN|UNKNOWN|WRONG-NAMESPACE|REVIEWER-WRITE-REFUSED|"
    r"REVIEWER-READ|TEST-ADMIN)"
)
REJECT_FIXTURE = {"MANUAL-APPLY": "unsupported_edits"}
APPLY_FIXTURE = {
    "ALT": "alt_supported",
    "RENAME": "rename_references",
    "LINK": "link_supported",
    "HEADER-RANGE": "header_range",
    "HEADER-TABLE": "header_disabled_table",
    "COMBINED": "mixed_decisions",
    "STRING-LABEL": "link_supported",
}
SIMPLE: dict[str, tuple[str, dict[str, Any]]] = {
    "P4-FAULT-SETUP": ("fault_setup", {}),
    "P3-AUDIT-FAILURE-ENVELOPE": ("fault_envelope", {"fixture": "clean_all_six"}),
    "P4-FAULT-RECOVERY": ("fault_recovery", {"fixture": "clean_all_six"}),
    "P4-FAULT-AUTH": ("fault_auth", {}),
    "P4-FAULT-INVALID": ("fault_invalid", {}),
    "P4-UPLOAD-ALL-SIX": ("fixture_upload", {"fixture": "seeded_all_six"}),
    "P4-AUDIT-ALL-SIX": ("fixture_audit", {"fixture": "seeded_all_six"}),
    "P4-CLEAN-ALL-SIX": ("fixture_upload_audit", {"fixture": "clean_all_six"}),
    "P4-MANUAL-ONLY-STATUS": ("fixture_upload_audit", {"fixture": "review_only"}),
    "P4-REVIEW-CONTEXT": ("review_context", {"fixture": "mixed_decisions"}),
    "P4-REVIEW-REVIEWER": ("review_reviewer", {"fixture": "mixed_decisions"}),
    "P4-VERSION-NEW": ("version_new", {"fixture": "clean_all_six", "changed": "seeded_all_six"}),
    "P4-VERSION-SAME-BYTES": ("version_same_bytes", {"fixture": "clean_all_six"}),
    "P4-REAUDIT": ("reaudit", {"fixture": "seeded_all_six"}),
    "P4-REAUDIT-INVALID": ("reaudit_invalid", {"fixture": "clean_all_six"}),
    "P4-REAUDIT-DETERMINISM": ("reaudit_determinism", {"fixture": "seeded_all_six"}),
    "P4-SOURCE-BEFORE": ("source_before", {"fixture": "preservation_rich"}),
    "P4-DECISIONS-MIXED": ("decisions_mixed", {"fixture": "mixed_decisions"}),
    "P4-APPLY-NO-EDITS": ("apply_no_edits", {"fixture": "review_only"}),
    "P4-BATCH-ATOMIC-REJECTION": ("batch_atomic", {"fixture": "mixed_decisions"}),
    "P4-IDEMPOTENCY-REPLAY": ("idempotency", {"fixture": "mixed_decisions", "case": "replay"}),
    "P4-IDEMPOTENCY-CONFLICT": ("idempotency", {"fixture": "mixed_decisions", "case": "conflict"}),
    "P4-IDEMPOTENCY-INVALID-FIRST": (
        "idempotency",
        {"fixture": "mixed_decisions", "case": "invalid_first"},
    ),
    "P4-OUTPUT-HASH": ("output_hash", {"fixture": "preservation_rich"}),
    "P4-SOURCE-AFTER": ("source_after", {"fixture": "preservation_rich"}),
    "P4-PRESERVATION": ("preservation", {"fixture": "preservation_rich"}),
    "P4-OUTPUT-AUDIT": ("output_audit", {"fixture": "mixed_decisions"}),
    "P4-REPORT-COMPLETE": ("report_complete", {"fixture": "mixed_decisions"}),
    "P4-REPORT-STABLE": ("report_stable", {"fixture": "mixed_decisions"}),
    "P4-HISTORY": ("history", {"fixture": "mixed_decisions"}),
    "P4-REMEDIATED-STATUS": ("remediated_status", {"fixture": "mixed_decisions"}),
    "P4-DOWNLOAD-LARGE": ("download_large", {"fixture": "large_preservation"}),
    "P4-SESSION-COVERAGE": ("session_coverage", {}),
    "P4-SESSION-CLEANUP": ("session_cleanup", {"fixture": "clean_all_six"}),
    "P4-REPORT-FORMAT": ("report_format", {}),
}
ERROR_PATH = re.compile(
    r"REJECT|-UNAUTHENTICATED|-INVALID-TOKEN|-FOREIGN|-UNKNOWN|-WRONG-NAMESPACE|-WRITE-REFUSED|"
    r"-TEST-ADMIN|REAUDIT-INVALID|IDEMPOTENCY-CONFLICT|BATCH-ATOMIC|FAULT-AUTH|FAULT-INVALID|"
    r"FAILURE-ENVELOPE"
)


def slug(text: str) -> str:
    return text.lower().replace("-", "_")


def bind(entry: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    test_id: str = entry["test_id"]
    if entry.get("baseline_check"):
        return entry["baseline_check"], dict(entry.get("inputs") or {})
    if test_id in SIMPLE:
        check, parameters = SIMPLE[test_id]
        return check, dict(parameters)
    if test_id.startswith("P4-RULE-"):
        case = test_id.removeprefix("P4-RULE-")
        return "rule_vector", {"fixture": entry["fixtures"][0], "case": slug(case)}
    if test_id.startswith("P4-APPLY-"):
        plan = test_id.removeprefix("P4-APPLY-")
        return "apply_correction", {"fixture": APPLY_FIXTURE[plan], "plan": slug(plan)}
    if test_id.startswith("P4-REJECT-"):
        condition = test_id.removeprefix("P4-REJECT-")
        fixture = REJECT_FIXTURE.get(condition, "mixed_decisions")
        return "reject_decisions", {"fixture": fixture, "condition": slug(condition)}
    match = ACCESS.fullmatch(test_id)
    if match:
        return "access", {
            "fixture": "mixed_decisions",
            "resource": slug(match.group(1)),
            "variant": slug(match.group(2)),
        }
    raise SystemExit(f"no handler bound for {test_id}")


def first_sentence(text: str) -> str:
    sentence = text.split(". ")[0].rstrip(".")
    return sentence if len(sentence) <= 160 else sentence[:157] + "..."


def build() -> dict[str, Any]:
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    phase3 = json.loads(PHASE3_MANIFEST.read_text(encoding="utf-8"))
    phase3_paths = {test["test_id"]: test.get("path") for test in phase3["tests"]}
    phase3_ops = {operation["number"]: operation for operation in phase3["operations"]}
    if plan["specification_version"] != "2.1.0" or plan["ruleset_version"] != "2.0.0":
        raise SystemExit("unexpected test plan version")
    pack_hashes = {}
    for line in (PLAN.parent / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        digest, filename = line.split(maxsplit=1)
        filename = filename.lstrip("*")
        if hashlib.sha256((PLAN.parent / filename).read_bytes()).hexdigest() != digest:
            raise SystemExit("corrected pack member hash differs: " + filename)
        pack_hashes[filename] = digest
    expectations = json.loads((OUTPUT.parent / "expectations-phase4.json").read_text("utf-8"))
    operations = []
    for number in [*range(1, 15), "S", "V", "R"]:
        delivered = number in PHASE4_OPERATIONS
        if isinstance(number, int):
            base = phase3_ops[number]
            method, path, name = PHASE4_OPERATIONS.get(
                number, (base["method"], base["path"], base["name"])
            )
            requirements = base["requirements"]
        else:
            method, path, name = PHASE4_OPERATIONS[number]
            requirements = ADDED_REQUIREMENTS[number]
        operations.append(
            {
                "number": number,
                "name": name,
                "method": method,
                "path": path,
                "requirements": requirements,
                "phase3": bool(isinstance(number, int) and phase3_ops[number]["phase3"]),
                "phase4": delivered,
                "note": (
                    "Specification 2.1.0 section 5; delivered in Phase 4."
                    if delivered
                    else "Phase 5 operation; Phase 4 returns the normal project 404."
                ),
            }
        )
    tests = []
    for entry in plan["tests"]:
        if entry["tier"] != "hosted_http":
            continue
        check, parameters = bind(entry)
        test_id = entry["test_id"]
        if entry["operations"]:
            path = phase3_paths.get(test_id) or (
                "error" if ERROR_PATH.search(test_id) else "success"
            )
        else:
            path = None
        test: dict[str, Any] = {
            "test_id": test_id,
            "check": check,
            "description": first_sentence(entry["qualifying_assertion"]),
            "qualifying_assertion": entry["qualifying_assertion"],
            "components": entry["components"],
            "acceptance": entry["acceptance"],
            "operations": entry["operations"],
            "path": path,
            "phase": "4",
            "prerequisites": entry["prerequisites"],
            "parameters": parameters,
            "fixtures": entry["fixtures"],
        }
        if path is None:
            del test["path"]
        selected = variants(test_id)
        if not selected:
            tests.append(test)
            continue
        for index, variant in enumerate(selected):
            row = {**test, "parameters": {**parameters, "variant": variant}}
            row["test_id"] = (
                test_id if index == 0 else test_id + "--" + variant.upper().replace("_", "-")
            )
            row["description"] = first_sentence(entry["qualifying_assertion"]) + f" [{variant}]"
            row["qualifying_assertion"] = (
                f"Individual {variant} vector from {test_id}: " + entry["qualifying_assertion"]
            )
            if test_id == "P4-RULE-COLOR-THEME-TINT" and variant != "standard":
                row["parameters"] = {"fixture": "tint_half_vectors", "case": "tint_" + variant}
                row["fixtures"] = ["tint_half_vectors"]
            tests.append(row)
    return {
        "manifest_version": MANIFEST_VERSION,
        "ruleset_version": plan["ruleset_version"],
        "specification_version": plan["specification_version"],
        "expectations": "expectations-phase4.json",
        "fixture_hashes": {
            name: fixture["sha256"]
            for name, fixture in expectations["fixtures"].items()
            if fixture["kind"] != "pending"
        },
        "pack_revision": "2.1.1",
        "pack_hashes": pack_hashes,
        "components": COMPONENTS,
        "acceptance": plan["acceptance"],
        "operations": operations,
        "limits": LIMITS,
        "pending_confirmation": [
            "Expanded vector IDs are recorded individually in suite 2.1.1; cross-team adoption "
            "of this executable manifest and its exact fixture hashes remains pending.",
            "Team 12's received clean.xlsx is not adopted: an independent reading finds six "
            "findings (docs/evidence/phase4_team12_clean_review.md).",
            "LINK_TEXT destination comparison uses the trimmed, collapsed, casefolded label "
            "against the trimmed, casefolded destination (tester/phase4/policy.py).",
            "APPLY to COLOR_CONTRAST/MERGED_CELLS findings expects 422 UNSUPPORTED_CORRECTION "
            "(safe-profile check before value shape); the pod must confirm the check order.",
            "HYPERLINK formula links target the indexed c element (#xml=...); an in-cell "
            "picture inside a dense range makes TABLE_HEADER manual. Pod confirmation needed.",
            "Cross-sheet conditional formulas live in the x14 extension (xm:f) and are "
            "rewritten by renames like main-namespace formulas.",
            "A STALE_AUDIT answer while the competing re-audit is still PROCESSING is a fail; "
            "if the re-audit already finished, the case is skipped as unobservable.",
        ],
        "known_deviations": [
            "HTTP-only run: C9 review UI and Excel no-repair behavior are not established here.",
            "Local-integration (24) and manual-release (10) plan entries are outside this report.",
            "Human fixture/Excel review was not performed; no approval is inferred "
            "from automated results.",
        ],
        "tests": tests,
    }


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed file differs")
    args = parser.parse_args()
    text = render(build())
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != text:
            raise SystemExit(f"stale Phase 4 manifest: {OUTPUT}")
    else:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(text, encoding="utf-8")
    tests = json.loads(text)["tests"]
    print(f"{len(tests)} hosted cases bound to {len({t['check'] for t in tests})} checks")


if __name__ == "__main__":
    main()
