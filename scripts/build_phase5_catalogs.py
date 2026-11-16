"""Build (or --check) the Phase 5 coverage catalogs and requirements-to-tests mapping.

Writes docs/testing/phase5-coverage-catalogs.md from the frozen runnable manifest, the
normative 3.0.0 plan and Team 9's design map, so the catalogs cannot drift from what the
tester actually counts. Nothing here reads a run result: it states denominators, qualifying
rules and mappings, never coverage achieved.
"""

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "tests" / "fixtures" / "manifest-phase5.json"
PLAN = REPO / "docs" / "contracts" / "phase5" / "phase5-test-plan.json"
DESIGN = REPO / "docs" / "design" / "team9-design-map.json"
OUTPUT = REPO / "docs" / "testing" / "phase5-coverage-catalogs.md"
UNIT = "tests/unit/test_phase5_tester.py"

# Specification 3.0.0 section 15 qualifying assertions.
QUALIFYING = {
    "C1": "Evaluate an actual allow/deny boundary",
    "C2": "Evaluate accepted/rejected workbook structure or limits",
    "C3": "Compare retained bytes/hash or version identity",
    "C4": "Evaluate exact expected findings or deterministic results",
    "C5": "Evaluate processing, terminal state or recovery",
    "C6": "Evaluate isolation or real measured session report",
    "C7": "Evaluate approved/rejected decisions or actual edit effects",
    "C8": "Evaluate returned artifact or historical record",
    "C9": "Execute retained browser/keyboard/accessibility assertions",
    "C10": "Evaluate cadence, dispatch or isolated time behavior",
    "C11": "Evaluate eligibility, occurrence or delivery record",
    "C12": "Evaluate owner deletion, removal, fencing or recovery",
}
# Local entries the tester itself owns, and the pytest cases that evaluate them.
TESTER_LOCAL = {
    "P5-TESTER-TRUSTED-IDENTITY": [
        "test_trusted_identity_classifies_own_and_cross",
        "test_caller_cannot_supply_a_profile_override",
    ],
    "P5-TESTER-MISSING-MAP": [
        "test_own_team_setup_without_a_valid_design_map_fails_before_ready",
        "test_design_map_with_unplanned_test_ids_is_rejected",
    ],
    "P5-TESTER-UNTESTED-DESIGN": ["test_model_coverage_counts_every_requirement_once"],
    "P5-TESTER-NO-DOUBLE-COUNT": ["test_model_coverage_counts_every_requirement_once"],
    "P5-TESTER-NO-SETUP-CREDIT": ["test_setup_skips_and_drains_earn_no_coverage"],
    "P5-TESTER-UNAVAILABLE": ["test_unavailable_code_coverage_is_null_not_zero"],
    "P5-TESTER-ERROR-SOURCE": [
        "test_outcome_vectors_follow_the_instructor_rule",
        "test_unexpected_scheduled_processing_failure_is_error_source_system",
        "test_unexpected_failure_keeps_phase4_rule_for_phase4_runs",
    ],
    "P5-TESTER-EVALUABLE-FAIL": [
        "test_defective_targets_fail_the_relevant_cases",
        "test_incorrectly_accepted_clock_work_is_drained_without_masking_the_failure",
    ],
    "P5-TESTER-ABORT": ["test_abort_and_recovery_keep_the_frozen_profile"],
    "P5-TESTER-ONE-RUN": [
        "test_abort_and_recovery_keep_the_frozen_profile",
        "test_full_own_team_run_against_a_conforming_fake",
    ],
    "P5-TESTER-SETUP-FORWARD": ["test_system_setup_errors_are_forwarded_unchanged"],
    "P5-TESTER-EVERY-ID": ["test_full_own_team_run_against_a_conforming_fake"],
    "P5-LOCAL-SCHEMA-PROFILES": [
        "test_profiles_are_enforced_by_normal_abort_and_recovery_builders"
    ],
    "P5-LOCAL-MANIFEST-GENERATION": ["test_committed_phase5_manifest_is_generated_from_the_plan"],
}
TARGET_SIDE_NOTE = {
    "P5-TESTER-DIRECT-STAFF": "System setup/report behavior for a staff-supplied session ID.",
    "P5-TESTER-REPORT-READONLY": (
        "System-side measurement; the hosted P4-SESSION-COVERAGE and P5-TESTER-CODE-COPY cases "
        "read the report twice and require identical copies."
    ),
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def ids(values: list[str], limit: int = 12) -> str:
    shown = ", ".join(f"`{value}`" for value in values[:limit])
    return shown + (f" and {len(values) - limit} more" if len(values) > limit else "") or "none"


def build() -> str:
    manifest = json.loads(MANIFEST.read_text("utf-8"))
    plan = json.loads(PLAN.read_text("utf-8"))
    design = json.loads(DESIGN.read_text("utf-8"))
    hosted = manifest["tests"]
    by_component: dict[str, list[str]] = defaultdict(list)
    by_criterion: dict[str, list[str]] = defaultdict(list)
    for test in hosted:
        for component in test["components"]:
            by_component[component].append(test["test_id"])
        for criterion in test["acceptance"]:
            by_criterion[criterion].append(test["test_id"])
    other: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for entry in plan["tests"]:
        if entry["tier"] != "hosted_http":
            for criterion in entry["acceptance"] or ["(none)"]:
                other[criterion][entry["tier"]].append(entry["test_id"])
    lines = [
        "# Phase 5 coverage catalogs and design-requirements-to-tests mapping",
        "",
        "Generated by `python -m scripts.build_phase5_catalogs`; CI fails if it is stale. Do not",
        "edit by hand. This file states denominators, qualifying rules and mappings. It records",
        "no run result and claims no coverage achieved.",
        "",
        "| Input | Version | SHA256 |",
        "|---|---|---|",
        f"| Runnable manifest `tests/fixtures/manifest-phase5.json` | "
        f"{manifest['manifest_version']} | `{digest(MANIFEST)}` |",
        f"| Normative plan `docs/contracts/phase5/phase5-test-plan.json` | "
        f"{plan['specification_version']} | `{digest(PLAN)}` |",
        f"| Team 9 design map `docs/design/team9-design-map.json` | {design['designVersion']} | "
        f"`{digest(DESIGN)}` |",
        "",
        "## How the tester counts (specification 3.0.0 section 14)",
        "",
        "- A component, criterion or design requirement counts once when a planned test executed",
        "  one of its predeclared qualifying assertions in this session, even if that assertion",
        "  failed. Coverage means evaluated, not passed or accepted.",
        "- Setup, preparation (fresh scenario namespaces, uploads, baseline schedules), the drain",
        "  of unexpectedly accepted work, skipped tests and mere test presence earn nothing.",
        "- Hosted runs are HTTP-only: C9 and AC-17 are never counted, so component coverage is at",
        "  most 11/12 and system coverage at most 17/18.",
        "- Own-team runs add `coverage.model` over every design requirement below, including",
        "  untested ones. Cross-team runs omit the key entirely. Trusted tester configuration,",
        "  not the grader request, decides the profile.",
        "- Tester self-checks of report shape (`P5-TESTER-SELF-MODEL`, `-CROSS-OMIT`,",
        "  `-SYSTEM-18`, `-COMPONENT-12`, `-STORED-BYTES`) evaluate the tester, not the target,",
        "  so they claim no C6 credit; `P5-TESTER-CODE-COPY` reads the target's session report and",
        "  does.",
        "",
        "## Component catalog (denominator 12)",
        "",
        "| ID | Shared component | Qualifying assertion | Hosted tests |",
        "|---|---|---|---|",
    ]
    for component, name in manifest["components"].items():
        tests = by_component.get(component, [])
        note = " (never counted over HTTP)" if component == "C9" else ""
        lines.append(
            f"| {component} | {cell(name)}{note} | {QUALIFYING[component]} | "
            f"{len(tests)}: {ids(tests, 6)} |"
        )
    lines += [
        "",
        "## System catalog (denominator 18)",
        "",
        "| AC | Phase 5 evidence scope | Hosted tests | Local / manual plan entries |",
        "|---|---|---|---|",
    ]
    for criterion, scope in manifest["acceptance"].items():
        tests = by_criterion.get(criterion, [])
        rest = other.get(criterion, {})
        side = "; ".join(f"{tier}: {ids(values, 4)}" for tier, values in sorted(rest.items()))
        lines.append(
            f"| {criterion} | {cell(scope)} | {len(tests)}: {ids(tests, 5)} | {side or 'none'} |"
        )
    lines += [
        "",
        f"## Team 9 design requirements to tests (design {design['designVersion']})",
        "",
        f"Stable mapping link: {design['mappingUrl']}",
        "",
        "Draft for team confirmation: the requirement list is derived from Team 9's delivered",
        "architecture and the Phase 5 obligations it implements. Untested requirements stay in",
        "the denominator. See docs/design/README.md.",
        "",
        "| Requirement | Description | Qualifying assertion | Mapped tests |",
        "|---|---|---|---|",
    ]
    for requirement in design["requirements"]:
        lines.append(
            f"| {requirement['requirementId']} | {cell(requirement['description'])} | "
            f"{cell(requirement['qualifyingAssertion'])} | "
            f"{ids(requirement['testIds'], 8) if requirement['testIds'] else 'untested'} |"
        )
    lines += [
        "",
        "## Non-hosted plan entries",
        "",
        "Outside the hosted SuiteReport. Tester-side entries run as pytest cases in",
        f"`{UNIT}`; the rest are target local integration or release evidence for their owners.",
        "",
        "| Entry | Tier | Where it is evaluated |",
        "|---|---|---|",
    ]
    for entry in plan["tests"]:
        if entry["tier"] == "hosted_http":
            continue
        test_id = entry["test_id"]
        if test_id in TESTER_LOCAL:
            where = "tester pytest: " + ", ".join(f"`{name}`" for name in TESTER_LOCAL[test_id])
        elif test_id in TARGET_SIDE_NOTE:
            where = "target local evidence. " + TARGET_SIDE_NOTE[test_id]
        elif entry["tier"] == "manual_release":
            where = "manual release evidence (pending)"
        else:
            where = "target local integration (system owners; pending)"
        lines.append(f"| `{test_id}` | {entry['tier']} | {where} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed file differs")
    args = parser.parse_args()
    text = build()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text("utf-8") != text:
            raise SystemExit(f"stale Phase 5 coverage catalogs: {OUTPUT}")
        return
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(text, "utf-8")
    print(f"wrote {OUTPUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
