"""Check the tester's Phase 5 oracles against every frozen vector and example of pack 3.0.0.

Covers the calendar policy (intervals, cadence, initial due, UTC reminder thresholds,
eligibility and crossing), the own/cross report profiles, and every valid/invalid example of
`phase5-examples.json` against the 3.0.0 wire schemas plus the tester's semantic checks. Uses
no network, no system code and no running service.
"""

import json
from pathlib import Path
from typing import Any

from tester.phase4 import contract
from tester.phase5 import policy
from tester.phase5.profile import check_report_profile

PACK = Path(__file__).resolve().parents[1] / "docs" / "contracts" / "phase5"
VECTORS = PACK / "phase5-policy-vectors.json"
EXAMPLES = PACK / "phase5-examples.json"


def check_policy(vectors: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if vectors.get("specification_version") != "3.0.0":
        problems.append("policy vectors are not specification 3.0.0")
    if policy.parse_timestamp(vectors["clock_initial_default"]) != policy.DEFAULT_CLOCK:
        problems.append("default clock differs")
    for item in vectors["intervals"]:
        if policy.is_valid_interval(item["intervalDays"]) != item["valid"]:
            problems.append(f"interval vector {item['name']}")
    for item in vectors["cadence"]:
        audits, nxt = policy.dispatch(
            policy.parse_timestamp(item["due"]),
            item["intervalDays"],
            policy.parse_timestamp(item["now"]),
        )
        if (audits, nxt) != (item["audits"], policy.parse_timestamp(item["nextAuditAt"])):
            problems.append(f"cadence vector {item['name']}")
    for item in vectors["initial_due"]:
        due = policy.initial_due(policy.parse_timestamp(item["now"]), item["intervalDays"])
        if due != policy.parse_timestamp(item["expected"]):
            problems.append(f"initial due from {item['now']}")
    for item in vectors["reminder_dates"]:
        instants = policy.reminder_instants(item["deadline"])
        for offset, expected in item["expected"].items():
            if instants[int(offset)] != policy.parse_timestamp(expected):
                problems.append(f"reminder date {item['deadline']} offset {offset}")
    for item in vectors["reminder_eligibility"]:
        if policy.eligible(item["latestStatus"], item["findingCount"]) != item["eligible"]:
            problems.append(f"eligibility {item['latestStatus']}/{item['findingCount']}")
    for item in vectors["reminder_crossing"]:
        offsets = policy.crossed_offsets(
            policy.parse_timestamp(item["configuredAt"]),
            policy.parse_timestamp(item["new"]),
            item["deadline"],
            set(item["alreadyConsidered"]),
        )
        if offsets != item["expectedOffsets"]:
            problems.append(f"crossing vector {item['name']}")
    profiles = vectors["profiles"]
    if profiles["own"]["required"] != ["code", "component", "system", "model"]:
        problems.append("own profile vector changed")
    if profiles["cross"]["forbidden"] != ["model"]:
        problems.append("cross profile vector changed")
    return problems


def _semantic_ok(definition: str, value: Any) -> bool:
    """Tester-side application checks that JSON Schema alone cannot express."""
    if definition == "ScheduleRequest" and value.get("enabled") is True:
        return policy.is_valid_interval(value.get("intervalDays"))
    if definition == "FaultConfiguration":
        return all(type(count) is int and count == 1 for count in value.values())
    return True


def check_examples(examples: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    with contract.use_pack("phase5"):
        for index, item in enumerate(examples["examples"]):
            definition, value = item["definition"], item["value"]
            found = contract.violations(definition, value)
            if found or not _semantic_ok(definition, value):
                problems.append(f"valid example {index} ({definition}) rejected")
            if definition in ("OwnTeamSuiteReport", "CrossTeamSuiteReport"):
                try:
                    check_report_profile(value, "own" if definition.startswith("Own") else "cross")
                except ValueError:
                    problems.append(f"valid example {index} violates its profile")
        for index, item in enumerate(examples["invalid_examples"]):
            definition, value = item["definition"], item["value"]
            rejected = bool(contract.violations(definition, value)) or not _semantic_ok(
                definition, value
            )
            if definition in ("OwnTeamSuiteReport", "CrossTeamSuiteReport"):
                try:
                    check_report_profile(value, "own" if definition.startswith("Own") else "cross")
                except ValueError:
                    rejected = True
            if not rejected:
                problems.append(f"invalid example {index} ({definition}) accepted")
    return problems


def main() -> None:
    vectors = json.loads(VECTORS.read_text(encoding="utf-8"))
    examples = json.loads(EXAMPLES.read_text(encoding="utf-8"))
    problems = check_policy(vectors) + check_examples(examples)
    if problems:
        raise SystemExit("Phase 5 policy/example check failed: " + "; ".join(problems))
    with contract.use_pack("phase5"):
        count = len(contract.definitions())
    print(
        f"Phase 5 policy vectors and {len(examples['examples'])} valid / "
        f"{len(examples['invalid_examples'])} invalid examples agree; {count} wire definitions"
    )


if __name__ == "__main__":
    main()
