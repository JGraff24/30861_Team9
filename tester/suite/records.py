"""Sanitized run records and the 14-operation status artifact, derived only from a stored report.

Records contain no tokens and no uploaded bytes. Failure classification (system, test or
specification) is a human judgement, so it starts as "unclassified" for the operator to fill in.
"""

import hashlib
from collections import Counter
from datetime import UTC, datetime
from typing import Any, Literal

from app.contracts.models import Outcome, SuiteReport
from tester.suite.manifest import Operation, SuiteManifest

Conformance = Literal["yes", "no", "incomplete", "untested"]


def _conformance(outcomes: list[Outcome]) -> Conformance:
    if Outcome.FAIL in outcomes:
        return "no"
    if Outcome.PASS not in outcomes:
        return "untested"
    if any(outcome in (Outcome.ERROR, Outcome.SKIP) for outcome in outcomes):
        return "incomplete"
    return "yes"


def operation_status(manifest: SuiteManifest, report: SuiteReport) -> list[dict[str, Any]]:
    outcomes = {result.test_id: result.outcome for result in report.results}
    rows = []
    for operation in manifest.operations:
        tests = [test for test in manifest.tests if operation.number in test.operations]
        by_path = {
            path: [outcomes[t.test_id] for t in tests if t.path == path and t.test_id in outcomes]
            for path in ("success", "error")
        }
        observed = any(
            outcome in (Outcome.PASS, Outcome.FAIL)
            for values in by_path.values()
            for outcome in values
        )
        rows.append(
            {
                **_describe(operation),
                "implemented": (
                    ("observed responding" if observed else "not observed")
                    if operation.phase3
                    else "not delivered in Phase 3"
                ),
                "success_conformance": _conformance(by_path["success"]),
                "error_conformance": _conformance(by_path["error"]),
                "evidence": [
                    {"test_id": t.test_id, "path": t.path, "outcome": outcomes.get(t.test_id)}
                    for t in tests
                ],
            }
        )
    return rows


def _describe(operation: Operation) -> dict[str, Any]:
    return operation.model_dump(mode="json")


def run_record(
    report_bytes: bytes,
    manifest: SuiteManifest,
    *,
    tester_url: str,
    target_revision: str,
    recorded_at: datetime | None = None,
) -> dict[str, Any]:
    report = SuiteReport.model_validate_json(report_bytes)
    counts = Counter(result.outcome.value for result in report.results)
    return {
        "recorded_at": (recorded_at or datetime.now(UTC)).isoformat().replace("+00:00", "Z"),
        "tester_url": tester_url,
        "target_url": report.target_url,
        # Supplied by the operator from pod documentation; never inferred from responses.
        "target_revision": target_revision,
        "session_id": report.session_id,
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "manifest_version": manifest.manifest_version,
        "counts": {
            "run": len(report.results),
            **{outcome.value: counts.get(outcome.value, 0) for outcome in Outcome},
        },
        "not_passed": [
            {
                **result.model_dump(mode="json"),
                "classification": "unclassified",
            }
            for result in report.results
            if result.outcome != Outcome.PASS
        ],
        "coverage": report.coverage.model_dump(mode="json"),
        "operation_status": operation_status(manifest, report),
        "pending_confirmation": manifest.pending_confirmation,
    }
