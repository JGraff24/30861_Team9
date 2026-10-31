"""Sanitized run records and the 14-operation status artifact, derived only from a stored report.

Records contain no tokens and no uploaded bytes. Failure classification (system, test or
specification) is a human judgement, so it starts as "unclassified" for the operator to fill in.
"""

import hashlib
import json
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
    phase = manifest.phase
    rows = []
    for operation in manifest.operations:
        delivered = operation.phase4 if phase == "4" else operation.phase3
        tests = [test for test in manifest.tests if operation.number in test.operations]
        by_path = {
            path: [outcomes.get(t.test_id, Outcome.SKIP) for t in tests if t.path == path]
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
                    if delivered
                    else f"not delivered in Phase {phase}"
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
    tester_revision: str | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> dict[str, Any]:
    report = SuiteReport.model_validate_json(report_bytes)
    if {result.test_id for result in report.results} != {test.test_id for test in manifest.tests}:
        raise ValueError("stored report does not match the supplied suite manifest")
    counts = Counter(result.outcome.value for result in report.results)
    record: dict[str, Any] = {
        "recorded_at": (recorded_at or datetime.now(UTC)).isoformat().replace("+00:00", "Z"),
        "tester_url": tester_url,
        "target_url": report.target_url,
        # Supplied by the operator from pod documentation; never inferred from responses.
        "target_revision": target_revision,
        "session_id": report.session_id,
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "manifest_version": manifest.manifest_version,
        "specification_version": manifest.specification_version,
        "ruleset_version": manifest.ruleset_version,
        "counts": {
            "run": sum(result.outcome != Outcome.SKIP for result in report.results),
            "planned": len(manifest.tests),
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
        "acceptance_status": {
            criterion: {
                "scope": scope,
                "evidence": [
                    {
                        "test_id": test.test_id,
                        "outcome": next(
                            (
                                result.outcome.value
                                for result in report.results
                                if result.test_id == test.test_id
                            ),
                            "missing",
                        ),
                    }
                    for test in manifest.tests
                    if criterion in test.acceptance
                ],
                "completeness": (
                    "exercised-criterion evidence only; per-AC human evidence is tracked "
                    "separately and outcomes do not prove every boundary"
                    if manifest.phase == "4"
                    else "partial Phase 3 scope; outcomes do not prove every boundary"
                ),
            }
            for criterion, scope in manifest.acceptance.items()
        },
        "known_deviations": manifest.known_deviations
        or [
            "Team 9 profile/defaults require pod confirmation.",
            "Synthetic fixtures and message meaning require manual pod review.",
            (
                "Controlled system failures are exercised locally; "
                "AC-14 is skipped remotely unless observed."
            ),
        ],
        "pending_confirmation": manifest.pending_confirmation,
    }
    if manifest.phase == "4":
        canonical = json.dumps(
            manifest.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        record.update(
            {
                "manifest_sha256": hashlib.sha256(canonical).hexdigest(),
                "manifest_hash_basis": "canonical UTF-8 JSON of the validated immutable manifest",
                "fixture_hashes": manifest.fixture_hashes,
                "pack_revision": manifest.pack_revision,
                "pack_hashes": manifest.pack_hashes,
            }
        )
        if tester_revision is not None:
            record["tester_revision"] = tester_revision
        if started_at is not None and ended_at is not None:
            record["started_at"] = started_at.isoformat().replace("+00:00", "Z")
            record["ended_at"] = ended_at.isoformat().replace("+00:00", "Z")
            record["timing_basis"] = "caller-observed run request start and final report retrieval"
    return record
