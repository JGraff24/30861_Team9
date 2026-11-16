"""Build (or --check) the runnable Phase 5 manifest from the normative 3.0.0 test plan.

`docs/contracts/phase5/phase5-test-plan.json` is an assertion inventory, not a runnable suite
(specification 3.0.0 section 18). Every hosted entry is bound to one real handler with explicit
parameters. Inherited Phase 4 entries keep their IDs, order and the individually reported
variant expansion of suite 2.1.1; new entries follow in plan order. Local-integration and
manual-release entries stay outside the hosted report: the tester-side ones are pytest cases
(docs/testing/phase5-coverage-catalogs.md), the rest are target or release evidence.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from scripts.build_phase4_manifest import ERROR_PATH as PHASE4_ERROR_PATH
from scripts.build_phase4_manifest import PHASE4_OPERATIONS, first_sentence
from scripts.build_phase4_manifest import bind as bind_phase4
from tester.phase4.catalog import variants

REPO = Path(__file__).resolve().parents[1]
PACK = REPO / "docs" / "contracts" / "phase5"
PLAN = PACK / "phase5-test-plan.json"
PHASE3_MANIFEST = REPO / "tests" / "fixtures" / "manifest.json"
PHASE4_MANIFEST = REPO / "tests" / "fixtures" / "manifest-phase4.json"
OUTPUT = REPO / "tests" / "fixtures" / "manifest-phase5.json"
EXPECTATIONS = "expectations-phase5.json"
MANIFEST_VERSION = "3.0.0"

LIMITS = {
    "suite_seconds": 2400,
    "run_read_timeout_seconds": 2430,
    "audit_poll_seconds": 80,
    "remediation_poll_seconds": 210,
    "business_get_seconds": 10,
    "upload_seconds": 35,
    "download_seconds": 30,
    "system_setup_seconds": 30,
    "json_response_bytes": 67_108_864,
    "binary_response_bytes": 33_554_432,
    "delete_seconds": 30,
}
PHASE5_OPERATIONS: dict[int | str, tuple[str, str, str, list[str]]] = {
    11: ("PUT", "/api/v1/workbooks/{workbookId}/schedule", "Configure a schedule", ["REQ-12"]),
    12: ("GET", "/api/v1/workbooks/{workbookId}/notifications", "Read notifications", ["REQ-12"]),
    13: ("POST", "/api/v1/test/clock", "Advance an isolated test clock", ["REQ-12", "REQ-13"]),
    14: ("DELETE", "/api/v1/workbooks/{workbookId}", "Delete a workbook", ["REQ-14"]),
    "G": ("GET", "/api/v1/workbooks/{workbookId}/schedule", "Read a schedule", ["REQ-12"]),
}
COMPONENTS = {
    "C1": "Authentication and authorization",
    "C2": "Workbook intake",
    "C3": "Immutable bytes and version metadata",
    "C4": "Six-rule audit engine",
    "C5": "Durable audit and remediation jobs",
    "C6": "Test sessions and code measurement",
    "C7": "Review decisions and corrections",
    "C8": "Downloads reports and history",
    "C9": "User interface",
    "C10": "Periodic scheduling and clock",
    "C11": "Deadline reminders and notifications",
    "C12": "Workbook deletion",
}
# Specification 3.0.0 section 17, by criterion.
ACCEPTANCE = {
    "AC-01": "Re-run upload regression workflow and intake boundaries (section 17).",
    "AC-02": "Re-run supported-file rejection and intake regressions (section 17).",
    "AC-03": "Re-run immutable source and hash regressions (section 17).",
    "AC-04": "Re-run six-rule finding regressions; real-world precision study is manual.",
    "AC-05": "Re-run classification and finding-identity regressions (section 17).",
    "AC-06": "Re-run review-context regressions (section 17).",
    "AC-07": "Re-run decision and rejected-decision regressions (section 17).",
    "AC-08": "Re-run report regressions (section 17).",
    "AC-09": "Independent preservation; Excel no-repair checks remain manual (section 17).",
    "AC-10": "Re-run correction and download regressions (section 17).",
    "AC-11": "Re-run history regressions (section 17).",
    "AC-12": "Intervals 1/90, invalid types, due boundaries, disable, newest version, "
    "coalescing; restart/claim isolation is local evidence.",
    "AC-13": "Reminders at 90/30/7 UTC days, clean suppression, eligibility, dedup, deadline "
    "changes; external-version UI disclosure is manual.",
    "AC-14": "Controlled audit, remediation, report, storage and scheduled-audit failures.",
    "AC-15": "Owner/granted reviewer/foreign/unauthenticated/test-admin boundaries on every "
    "new operation and namespace.",
    "AC-16": "Complete owner deletion over HTTP; HTTPS, credential and backup/restore "
    "evidence is manual.",
    "AC-17": "Automated scan, keyboard workflow and contrast evidence (manual/browser only).",
    "AC-18": "Five real-time boundary runs with controlled outcomes and identical findings.",
}
SCHEDULE: dict[str, tuple[str, dict[str, Any]]] = {
    "P5-SCHEDULE-DEFAULT": ("schedule_default", {}),
    "P5-SCHEDULE-MIN": ("schedule_bound", {"interval": 1}),
    "P5-SCHEDULE-MAX": ("schedule_bound", {"interval": 90, "leap_clock": "2028-01-01T00:00:00Z"}),
    "P5-SCHEDULE-PUT-REPLAY": ("schedule_replay", {}),
    "P5-SCHEDULE-DEADLINE-ONLY": ("schedule_deadline_only", {}),
    "P5-SCHEDULE-INTERVAL-CHANGE": ("schedule_interval_change", {}),
    "P5-SCHEDULE-DISABLE": ("schedule_disable", {}),
    "P5-SCHEDULE-REENABLE": ("schedule_reenable", {}),
    "P5-SCHEDULE-BEFORE-DUE": ("schedule_before_due", {}),
    "P5-SCHEDULE-EXACT-DUE": ("schedule_exact_due", {}),
    "P5-SCHEDULE-COALESCE": ("schedule_coalesce", {}),
    "P5-SCHEDULE-SAME-TIME": ("schedule_same_time", {}),
    "P5-SCHEDULE-LATEST-UPLOAD": ("schedule_latest_upload", {"changed": "seeded_all_six"}),
    "P5-SCHEDULE-LATEST-OUTPUT": (
        "schedule_latest_output",
        {"fixture": "alt_supported", "plan": "alt"},
    ),
    "P5-SCHEDULE-OLD-REAUDIT": ("schedule_old_reaudit", {}),
    "P5-SCHEDULE-BUSY-AUDIT": (
        "schedule_busy",
        {"kind": "audit", "fixture": "large_preservation"},
    ),
    "P5-SCHEDULE-BUSY-REMEDIATION": (
        "schedule_busy",
        {"kind": "remediation", "fixture": "alt_supported", "plan": "alt"},
    ),
    "P5-SCHEDULE-TWO-WORKBOOKS": ("schedule_two_workbooks", {}),
    "P5-SCHEDULE-FAILED-NEXT": ("schedule_failed_next", {}),
    "P5-SCHEDULE-UNKNOWN-FIELD": ("schedule_reject", {"body": {"unknownField": 1}}),
}
EXTRA_FIXTURES = {
    "P5-SCHEDULE-LATEST-UPLOAD": ["seeded_all_six"],
    "P5-SCHEDULE-LATEST-OUTPUT": ["alt_supported"],
    "P5-SCHEDULE-BUSY-AUDIT": ["large_preservation"],
    "P5-SCHEDULE-BUSY-REMEDIATION": ["alt_supported"],
    "P5-REMINDER-NO-AUDIT": ["large_preservation"],
    "P5-REMINDER-PROCESSING": ["large_preservation"],
    "P5-REMINDER-DECLINED": ["review_only"],
    "P5-FAULT-REMEDIATION": ["alt_supported"],
    "P5-FAULT-REJECT-REJECT-NO-CONSUME": ["clean_all_six", "corrupt"],
    "P5-FAULT-REJECT-ORDINARY": ["clean_all_six"],
    "P5-FAULT-REJECT-OTHER-SESSION": ["clean_all_six"],
    "P5-FAULT-REJECT-REUSED-ID": ["clean_all_six"],
    "P5-DELETE-COMPLETE": ["alt_supported"],
    "P5-DELETE-OLD-URLS": ["alt_supported"],
    "P5-DELETE-ACTIVE-AUDIT": ["large_preservation"],
    "P5-DELETE-ACTIVE-REMEDIATION": ["alt_supported"],
    "P5-CLOCK-SAME": ["seeded_all_six"],
    "P5-CLOCK-ISOLATION": ["clean_all_six"],
    "P5-CLOCK-REWIND": ["clean_all_six"],
}
REMINDER = re.compile(r"P5-REMINDER-(.+)")
CLOCK = re.compile(r"P5-CLOCK-(.+)")
DELETE = re.compile(r"P5-DELETE-(.+)")
ACCESS = re.compile(r"P5-ACCESS-(SCHEDULE-WRITE|SCHEDULE-READ|NOTIFICATIONS|DELETE)-(.+)")
CLOCK_ACCESS = re.compile(r"P5-ACCESS-CLOCK-(.+)")
FAULT = re.compile(
    r"P5-FAULT-(AUDIT|REMEDIATION|REPORT|STORAGE|SCHEDULED_AUDIT|NOTIFICATION|DELETION)"
)
FAULT_REJECT = {
    "P5-FAULT-REJECT-UNKNOWN": "unknown",
    "P5-FAULT-REJECT-BOOLEAN": "boolean",
    "P5-FAULT-REJECT-EMPTY": "empty",
    "P5-FAULT-REJECT-DISABLED": "disabled",
    "P5-FAULT-REJECT-ORDINARY": "ordinary",
    "P5-FAULT-REJECT-OTHER-SESSION": "other_session",
    "P5-FAULT-REJECT-REUSED-ID": "reused_id",
    "P5-FAULT-REJECT-AUDIT-REPORT-COMBINATION": "combination",
    "P5-FAULT-REJECT-REJECT-NO-CONSUME": "no_consume",
}
TESTER = {
    "P5-TESTER-SELF-MODEL": "self_model",
    "P5-TESTER-CROSS-OMIT": "cross_omit",
    "P5-TESTER-SYSTEM-18": "system_18",
    "P5-TESTER-COMPONENT-12": "component_12",
    "P5-TESTER-CODE-COPY": "code_copy",
    "P5-TESTER-STORED-BYTES": "stored_bytes",
}
ERROR_PATH = re.compile(
    r"REJECT|-NO-TOKEN|-INVALID-TOKEN|-FOREIGN-OWNER|-WRONG-NAMESPACE|-TEST-ADMIN|"
    r"-UNKNOWN-RESOURCE|-UNGRANTED|-OWNER-USER$|-REVIEWER$|-FOREIGN-ADMIN|P5-FAULT-|BUSY|ACTIVE|"
    r"REWIND|CLOCK-UTC|CLOCK-DISABLED|CLOCK-CLEANED|CLOCK-NOT-READY|DELETE-REPEAT|FAIL-ONCE|"
    r"FAILED-NEXT|FAILED-RECORD"
)


def slug(text: str) -> str:
    return text.lower().replace("-", "_")


def bind_phase5(entry: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    test_id: str = entry["test_id"]
    inputs = dict(entry.get("inputs") or {})
    if test_id in SCHEDULE:
        check, parameters = SCHEDULE[test_id]
        return check, dict(parameters)
    if test_id.startswith("P5-SCHEDULE-REJECT-"):
        return "schedule_reject", {"body": inputs}
    if test_id == "P5-PERFORMANCE-COMPARE":
        return "performance_compare", {}
    if test_id.startswith("P5-PERFORMANCE-RUN-"):
        run = int(test_id.rsplit("-", 1)[1])
        return "performance_run", {"run": run, "fixture": "performance_250000_cells"}
    if test_id in TESTER:
        return "tester_report", {"case": TESTER[test_id]}
    if test_id in FAULT_REJECT:
        return "fault_reject", {"case": FAULT_REJECT[test_id]}
    match = FAULT.fullmatch(test_id)
    if match:
        (fault,) = inputs["setup"]["faults"]
        return "fault_p5", {"fault": fault}
    match = CLOCK_ACCESS.fullmatch(test_id)
    if match:
        return "clock_access", {
            "identity": slug(match.group(1)),
            "expected_status": inputs["expected_status"],
        }
    match = ACCESS.fullmatch(test_id)
    if match:
        return "access_p5", {
            "operation": slug(match.group(1)),
            "identity": slug(match.group(2)),
            "expected_status": inputs["expected_status"],
        }
    match = REMINDER.fullmatch(test_id)
    if match:
        return "reminder", {"case": slug(match.group(1))}
    match = CLOCK.fullmatch(test_id)
    if match:
        return "clock", {"case": slug(match.group(1))}
    match = DELETE.fullmatch(test_id)
    if match:
        return "deletion", {"case": slug(match.group(1))}
    raise SystemExit(f"no handler bound for {test_id}")


def pack_hashes() -> dict[str, str]:
    hashes = {}
    for line in (PACK / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        digest, filename = line.split(maxsplit=1)
        filename = filename.lstrip("*")
        if hashlib.sha256((PACK / filename).read_bytes()).hexdigest() != digest:
            raise SystemExit("adopted pack member hash differs: " + filename)
        hashes[filename] = digest
    return hashes


def operations() -> list[dict[str, Any]]:
    phase3 = json.loads(PHASE3_MANIFEST.read_text(encoding="utf-8"))
    phase4 = {
        operation["number"]: operation
        for operation in json.loads(PHASE4_MANIFEST.read_text(encoding="utf-8"))["operations"]
    }
    phase3_ops = {operation["number"]: operation for operation in phase3["operations"]}
    rows = []
    for number in [*range(1, 15), "S", "V", "R", "G"]:
        if number in PHASE5_OPERATIONS:
            method, path, name, requirements = PHASE5_OPERATIONS[number]
            note = "Specification 3.0.0 section 5; delivered in Phase 5."
            phase4_delivered = False
        else:
            base = phase4[number]
            method, path, name = PHASE4_OPERATIONS[number]
            requirements, note = (
                base["requirements"],
                "Specification 3.0.0 section 5; " + ("inherited Phase 4 semantics."),
            )
            phase4_delivered = True
        rows.append(
            {
                "number": number,
                "name": name,
                "method": method,
                "path": path,
                "requirements": requirements,
                "phase3": bool(isinstance(number, int) and phase3_ops[number]["phase3"]),
                "phase4": phase4_delivered,
                "phase5": True,
                "note": note,
            }
        )
    return rows


def row(
    entry: dict[str, Any], check: str, parameters: dict[str, Any], path: str | None
) -> dict[str, Any]:
    fixtures = list(dict.fromkeys([*entry["fixtures"], *EXTRA_FIXTURES.get(entry["test_id"], [])]))
    test: dict[str, Any] = {
        "test_id": entry["test_id"],
        "check": check,
        "description": first_sentence(entry["qualifying_assertion"]),
        "qualifying_assertion": entry["qualifying_assertion"],
        "components": entry["components"],
        "acceptance": entry["acceptance"],
        "operations": entry["operations"],
        "path": path,
        "phase": "5",
        "prerequisites": entry["prerequisites"],
        "parameters": parameters,
        "fixtures": fixtures,
    }
    if path is None:
        del test["path"]
    return test


def build() -> dict[str, Any]:
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    if plan["specification_version"] != "3.0.0" or plan["ruleset_version"] != "2.0.0":
        raise SystemExit("unexpected test plan version")
    phase3_paths = {
        test["test_id"]: test.get("path")
        for test in json.loads(PHASE3_MANIFEST.read_text(encoding="utf-8"))["tests"]
    }
    expectations = json.loads((OUTPUT.parent / EXPECTATIONS).read_text("utf-8"))
    tests = []
    for entry in plan["tests"]:
        if entry["tier"] != "hosted_http":
            continue
        inherited = "inherited_from" in entry
        test_id = entry["test_id"]
        check, parameters = (bind_phase4 if inherited else bind_phase5)(entry)
        path = None
        if entry["operations"]:
            if inherited:
                path = phase3_paths.get(test_id) or (
                    "error" if PHASE4_ERROR_PATH.search(test_id) else "success"
                )
            else:
                path = "error" if ERROR_PATH.search(test_id) else "success"
        base = row(entry, check, parameters, path)
        selected = variants(test_id) if inherited else ()
        if not selected:
            tests.append(base)
            continue
        for index, variant in enumerate(selected):
            expanded = {**base, "parameters": {**parameters, "variant": variant}}
            expanded["test_id"] = (
                test_id if index == 0 else test_id + "--" + variant.upper().replace("_", "-")
            )
            expanded["description"] = (
                first_sentence(entry["qualifying_assertion"]) + f" [{variant}]"
            )
            expanded["qualifying_assertion"] = (
                f"Individual {variant} vector from {test_id}: " + entry["qualifying_assertion"]
            )
            if test_id == "P4-RULE-COLOR-THEME-TINT" and variant != "standard":
                expanded["parameters"] = {"fixture": "tint_half_vectors", "case": "tint_" + variant}
                expanded["fixtures"] = ["tint_half_vectors"]
            tests.append(expanded)
    return {
        "manifest_version": MANIFEST_VERSION,
        "ruleset_version": plan["ruleset_version"],
        "specification_version": plan["specification_version"],
        "expectations": EXPECTATIONS,
        "fixture_hashes": {
            name: fixture["sha256"]
            for name, fixture in expectations["fixtures"].items()
            if fixture["kind"] != "pending"
        },
        "pack_revision": "3.0.0",
        "pack_hashes": pack_hashes(),
        "components": COMPONENTS,
        "acceptance": ACCEPTANCE,
        "operations": operations(),
        "limits": LIMITS,
        "pending_confirmation": [
            "Pod adoption of specification/pack 3.0.0 (ADOPTION.md) is not recorded; this "
            "executable manifest and its fixture hashes await Team 12 agreement.",
            "Reviewer access assumes the configured reviewer holds a read grant from userA only; "
            "the ungranted-reviewer cases read a userB workbook. Team 12 provisioning must match.",
            "Foreign test-admin clock cases need an optional second credential (test-adminB); "
            "without it they are reported as skips.",
            "Busy-version, active-deletion and PROCESSING-eligibility cases assert only when the "
            "active window is observed on both sides of the call; otherwise they skip as "
            "unobservable. A fast target may need target-side local evidence for these.",
            "P5-CLOCK-NOT-READY and P5-FAULT-REJECT-DISABLED are unobservable over HTTP on a "
            "deployment whose fault/clock capability is enabled; they report skips.",
            "Synchronous TEST_STORAGE_FAILURE details are not specified; only the code and "
            "ProjectError shape are asserted.",
            "Scheduled-audit, notification, report and remediation injected messages follow the "
            "section 11 pattern '<Operation> failure requested by the isolated test session.'",
            "AC-18 accepts any PROCESSING_FAILED code ending TIMEOUT as the controlled timeout "
            "alternative: 2.1.0 lists PROCESSING_TIMEOUT in an open list (Team 9 emits it). Any "
            "other failure of the boundary workbook is error/system. Pod should fix one code.",
            "Same-due scheduled audits are expected in workbookId order compared as plain "
            "code-point strings (section 9 does not state the comparison).",
            "Ordinary-namespace probes (fault/clock isolation) upload one clean workbook as userA "
            "and remove it with owner deletion; a failed deletion leaves it behind.",
            "Inherited Phase 4 pending items (link targets, check order, formula links, x14 "
            "rename, stale-audit observability) are unchanged; see manifest-phase4.json.",
        ],
        "known_deviations": [
            "HTTP-only run: C9 and AC-17 are never counted; Excel no-repair (AC-09), HTTPS/"
            "credential/backup (AC-16) and monitoring evidence are manual.",
            "Local-integration (57) and manual-release (21) plan entries are outside this "
            "report; tester-side ones are pytest cases, the rest are target/release evidence.",
            "AC-18 timings are tester-observed end-to-end real time; the target-side "
            "validation/queue/engine split is separate (Ryan's measured execution).",
            "Human fixture/Excel review was not performed; no approval is inferred from "
            "automated results.",
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
            raise SystemExit(f"stale Phase 5 manifest: {OUTPUT}")
    else:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(text, encoding="utf-8")
    tests = json.loads(text)["tests"]
    print(f"{len(tests)} hosted cases bound to {len({t['check'] for t in tests})} checks")


if __name__ == "__main__":
    main()
