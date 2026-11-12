"""Phase 5 tester: frozen pack, calendar oracle, manifest, report profiles and full runs.

Full runs use the test-only fake target in tests/support/fake_phase5.py. Defect switches prove
that the independent checks fail on real defects instead of passing vacuously. The test names
carry the local tester entries of the Phase 5 inventory (docs/testing/phase5-coverage-catalogs.md).
"""

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.contracts.models import CoverageMeasure, Outcome, SuiteReport
from scripts.build_phase5_manifest import build, render
from scripts.check_phase5_policy import EXAMPLES, VECTORS, check_examples, check_policy
from tester.config import TesterSettings as SettingsForTester
from tester.main import TesterServices as ServicesForTester
from tester.main import create_app
from tester.phase4 import contract
from tester.phase5 import policy
from tester.phase5.profile import (
    ProfileError,
    RunProfile,
    check_report_profile,
    classify,
    model_coverage,
)
from tester.suite import checks, runner
from tester.suite.manifest import SuiteManifest, load_suite
from tester.suite.records import run_record
from tester.suite.registry import names as check_names
from tests.support.fake_phase4 import BASE_URL
from tests.support.fake_phase5 import FakePhase5, access_map
from tests.support.fake_system import address_resolver

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"
MANIFEST = FIXTURES / "manifest-phase5.json"
DESIGN = REPO / "docs" / "design" / "team9-design-map.json"
PLAN = REPO / "docs" / "contracts" / "phase5" / "phase5-test-plan.json"
OPERATOR = "phase5-operator-token"
SMALL = {"upload_limit": 60_000, "worksheet_limit": 5, "cell_limit": 100}
UNOBSERVABLE = {"P5-CLOCK-NOT-READY", "P5-FAULT-REJECT-DISABLED"}


@pytest.fixture(autouse=True)
def small_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, item in SettingsForTester.model_fields.items():
        monkeypatch.delenv(str(item.validation_alias or name.upper()), raising=False)
    monkeypatch.setattr(checks, "UPLOAD_LIMIT_BYTES", SMALL["upload_limit"])
    monkeypatch.setattr(checks, "WORKSHEET_LIMIT", SMALL["worksheet_limit"])
    monkeypatch.setattr(checks, "NONEMPTY_CELL_LIMIT", SMALL["cell_limit"])


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def settings(tmp_path: Path, **overrides: Any) -> SettingsForTester:
    values: dict[str, Any] = {
        "tester_db_path": tmp_path / "tester.sqlite3",
        "allowed_target_urls": [BASE_URL],
        "TARGET_AUTH_MAP_JSON": access_map(),
        "TESTER_OPERATOR_TOKEN_HASHES_JSON": {hashlib.sha256(OPERATOR.encode()).hexdigest(): "op"},
        "fixture_manifest_path": MANIFEST,
        "tester_team_id": "team9",
        "TARGET_TEAM_MAP_JSON": {BASE_URL: "team9"},
        "tester_design_map_path": DESIGN,
    }
    values.update(overrides)
    return SettingsForTester(**values)


def client_for(tmp_path: Path, fake: FakePhase5, **overrides: Any) -> TestClient:
    clock = Clock()
    services = ServicesForTester(
        settings=settings(tmp_path, **overrides),
        transport=fake.transport(),
        resolver=address_resolver(),
        sleep=clock.sleep,
        clock=clock,
    )
    return TestClient(create_app(services=services))


HEADERS = {"Authorization": f"Bearer {OPERATOR}"}


def run_phase5(tmp_path: Path, fake: FakePhase5, **overrides: Any) -> tuple[dict[str, Any], bytes]:
    with client_for(tmp_path, fake, **overrides) as client:
        setup = client.post("/test/setup", json={"target_url": BASE_URL}, headers=HEADERS)
        assert setup.status_code == 201, setup.text
        session_id = setup.json()["session_id"]
        run = client.post("/test/run", json={"session_id": session_id}, headers=HEADERS)
        assert run.status_code == 200, run.text
        stored = client.get("/test/report", params={"session_id": session_id}, headers=HEADERS)
        assert stored.content == run.content
        assert (
            client.post("/test/run", json={"session_id": session_id}, headers=HEADERS).status_code
            == 409
        )
    return run.json(), run.content


def outcomes(report: dict[str, Any]) -> dict[str, str]:
    return {result["test_id"]: result["outcome"] for result in report["results"]}


# --- frozen pack and oracle ---------------------------------------------------------------------


def test_policy_vectors_and_examples_match_the_frozen_pack() -> None:
    assert check_policy(json.loads(VECTORS.read_text("utf-8"))) == []
    assert check_examples(json.loads(EXAMPLES.read_text("utf-8"))) == []


def test_adopted_pack_is_byte_identical() -> None:
    pack = REPO / "docs" / "contracts" / "phase5"
    for line in (pack / "SHA256SUMS.txt").read_text("utf-8").splitlines():
        digest, name = line.split(maxsplit=1)
        assert hashlib.sha256((pack / name).read_bytes()).hexdigest() == digest, name
    with contract.use_pack("phase5"):
        assert len(contract.definitions()) == 59
    assert len(contract.definitions()) == 44  # Phase 4 runs keep the 2.1.0 pack


def test_phase5_timestamps_and_calendar_arithmetic() -> None:
    assert policy.parse_timestamp("2027-01-01T23:59:59.999999Z").microsecond == 999_999
    for bad in (
        "2027-01-01T00:00:00.1234567Z",
        "2027-01-01T00:00:00+00:00",
        "2027-02-29T00:00:00Z",
    ):
        with pytest.raises(ValueError):
            policy.parse_timestamp(bad)
    leap = policy.reminder_instants("2028-04-26")
    assert [policy.format_timestamp(leap[o]) for o in (90, 30, 7)] == [  # type: ignore[arg-type]
        "2028-01-27T00:00:00Z",
        "2028-03-27T00:00:00Z",
        "2028-04-19T00:00:00Z",
    ]
    assert policy.reminder_instants("0001-02-01")[90] is None  # before year 0001
    assert not policy.is_valid_interval(True) and not policy.is_valid_interval(1.0)


def test_report_version_alias_only_applies_to_phase5_runs() -> None:
    report = json.loads((REPO / "docs/contracts/phase5/phase5-examples.json").read_text("utf-8"))
    new = next(x["value"] for x in report["examples"] if x["definition"] == "NewAuditReport")
    with contract.use_pack("phase5"):
        assert contract.violations("AuditReport", new) == []
        legacy = {**new, "specificationVersion": "2.1.0"}
        assert contract.violations("AuditReport", legacy)  # new runs require 3.0.0
    assert contract.violations("AuditReport", new)  # Phase 4 runs still require 2.1.0


# --- manifest and catalogs (P5-LOCAL-MANIFEST-GENERATION) ---------------------------------------


def test_committed_phase5_manifest_is_generated_from_the_plan() -> None:
    assert MANIFEST.read_text("utf-8") == render(build())
    manifest = SuiteManifest.model_validate_json(MANIFEST.read_bytes())
    plan = json.loads(PLAN.read_text("utf-8"))
    hosted = [entry["test_id"] for entry in plan["tests"] if entry["tier"] == "hosted_http"]
    phase4 = json.loads((FIXTURES / "manifest-phase4.json").read_text("utf-8"))
    ids = [test.test_id for test in manifest.tests]
    assert ids[: len(phase4["tests"])] == [test["test_id"] for test in phase4["tests"]]
    assert set(hosted) <= set(ids) and len(ids) == len(set(ids)) == 395
    assert list(manifest.components) == [f"C{n}" for n in range(1, 13)]
    assert list(manifest.acceptance) == [f"AC-{n:02d}" for n in range(1, 19)]
    assert manifest.limits is not None and manifest.limits.suite_seconds == 2400
    assert manifest.limits.run_read_timeout_seconds >= 2430
    assert {test.check for test in manifest.tests} <= check_names()
    for test in manifest.tests:
        entry = next(e for e in plan["tests"] if test.test_id.split("--")[0] == e["test_id"])
        assert (test.components, test.acceptance, test.operations) == (
            entry["components"],
            entry["acceptance"],
            entry["operations"],
        )


def test_phase5_suite_loads_with_generated_and_committed_fixtures() -> None:
    suite = load_suite(MANIFEST, check_names())
    data = suite.bytes_of("performance_250000_cells")
    assert len(data) > 6_000_000 and suite.manifest.phase == "5"
    assert hashlib.sha256(data).hexdigest() == suite.accepted("performance_250000_cells").sha256


# --- trusted profile and design model (P5-TESTER-TRUSTED-IDENTITY/MISSING-MAP/...) ----------


def test_trusted_identity_classifies_own_and_cross() -> None:
    ids = [test.test_id for test in SuiteManifest.model_validate_json(MANIFEST.read_bytes()).tests]
    own = classify(
        BASE_URL,
        tester_team="team9",
        target_teams={BASE_URL: "team9"},
        design_map_path=DESIGN,
        planned_ids=ids,
    )
    assert own.kind == "own" and own.design_map is not None
    cross = classify(
        BASE_URL,
        tester_team="team9",
        target_teams={BASE_URL: "team12"},
        design_map_path=None,
        planned_ids=ids,
    )
    assert cross.kind == "cross" and cross.design_map is None
    # Exact URL match only: a similar host is not classified.
    with pytest.raises(ProfileError):
        classify(
            BASE_URL + "/",
            tester_team="team9",
            target_teams={BASE_URL: "team9"},
            design_map_path=DESIGN,
            planned_ids=ids,
        )
    with pytest.raises(ProfileError):
        classify(
            BASE_URL, tester_team=None, target_teams={}, design_map_path=DESIGN, planned_ids=ids
        )


def test_caller_cannot_supply_a_profile_override(tmp_path: Path) -> None:
    with client_for(tmp_path, FakePhase5(**SMALL)) as client:
        response = client.post(
            "/test/setup", json={"target_url": BASE_URL, "profile": "cross"}, headers=HEADERS
        )
    assert response.status_code == 400 and response.json()["source"] == "tester"


@pytest.mark.parametrize(
    "overrides",
    [
        {"tester_design_map_path": None},
        {"tester_design_map_path": Path("/nonexistent/map.json")},
        {"tester_team_id": None},
        {"TARGET_TEAM_MAP_JSON": {}},
    ],
)
def test_own_team_setup_without_a_valid_design_map_fails_before_ready(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    fake = FakePhase5(**SMALL)
    with client_for(tmp_path, fake, **overrides) as client:
        response = client.post("/test/setup", json={"target_url": BASE_URL}, headers=HEADERS)
    assert response.status_code == 500
    assert response.json() == {
        "message": "Tester report profile configuration is missing or invalid.",
        "source": "tester",
    }
    assert not fake.sessions  # no target namespace was prepared


def test_design_map_with_unplanned_test_ids_is_rejected(tmp_path: Path) -> None:
    document = json.loads(DESIGN.read_text("utf-8"))
    document["requirements"][0]["testIds"].append("P5-NOT-A-PLANNED-TEST")
    bad = tmp_path / "map.json"
    bad.write_text(json.dumps(document), "utf-8")
    with client_for(tmp_path, FakePhase5(**SMALL), tester_design_map_path=bad) as client:
        response = client.post("/test/setup", json={"target_url": BASE_URL}, headers=HEADERS)
    assert response.status_code == 500 and response.json()["source"] == "tester"


def test_model_coverage_counts_every_requirement_once() -> None:
    document = {
        "teamId": "team9",
        "designVersion": "1.2.3",
        "mappingUrl": "https://example.invalid/map",
        "requirements": [
            {
                "requirementId": "D1",
                "description": "a",
                "testIds": ["T1", "T2"],
                "qualifyingAssertion": "x",
            },
            {
                "requirementId": "D2",
                "description": "b",
                "testIds": ["T2"],
                "qualifyingAssertion": "x",
            },
            {"requirementId": "D3", "description": "c", "testIds": [], "qualifyingAssertion": "x"},
            {
                "requirementId": "D4",
                "description": "d",
                "testIds": ["T9"],
                "qualifyingAssertion": "x",
            },
        ],
    }
    profile = RunProfile("own", "team9", "team9", document, "0" * 64)
    measure = model_coverage(profile, {"T1", "T2"})
    # D1 counts once despite two qualifying tests; untested D3 and unexecuted D4 stay in total.
    assert (measure.covered, measure.total) == (2, 4)
    assert "1.2.3" in measure.basis and "https://example.invalid/map" in measure.basis
    assert model_coverage(profile, set()).covered == 0


def test_profiles_are_enforced_by_normal_abort_and_recovery_builders(tmp_path: Path) -> None:
    """P5-LOCAL-SCHEMA-PROFILES: own requires model, cross forbids it, on every builder."""
    suite = load_suite(MANIFEST, check_names())
    ids = [test.test_id for test in suite.manifest.tests]
    for kind, team in (("own", "team9"), ("cross", "team12")):
        profile = classify(
            BASE_URL,
            tester_team="team9",
            target_teams={BASE_URL: team},
            design_map_path=DESIGN,
            planned_ids=ids,
        )
        restored = type(suite)(suite.manifest, suite.expectations, suite.data, profile.snapshot())
        partial = runner.interrupted_results(restored, [], "Tester aborted.")
        code = CoverageMeasure(covered=None, total=None, basis="Target code coverage unavailable")
        report = runner.build_report(restored, "s-1", BASE_URL, partial, code, {})
        body = json.loads(report.model_dump_json())
        check_report_profile(body, kind)  # type: ignore[arg-type]
        assert ("model" in body["coverage"]) == (kind == "own")
        other = "cross" if kind == "own" else "own"
        with pytest.raises(ValueError):
            check_report_profile(body, other)  # type: ignore[arg-type]


# --- full runs (P5-TESTER-EVERY-ID, SELF-MODEL, CROSS-OMIT, STORED-BYTES) -------------------------


def test_full_own_team_run_against_a_conforming_fake(tmp_path: Path) -> None:
    report, raw = run_phase5(tmp_path, FakePhase5(**SMALL))
    manifest = SuiteManifest.model_validate_json(MANIFEST.read_bytes())
    assert [r["test_id"] for r in report["results"]] == [t.test_id for t in manifest.tests]
    check_report_profile(report, "own")
    with contract.use_pack("phase5"):
        assert contract.violations("SuiteReport", report) == []
    result = outcomes(report)
    unexpected = {
        test_id: outcome
        for test_id, outcome in result.items()
        if outcome != "pass" and test_id not in UNOBSERVABLE | {"P5-TESTER-CROSS-OMIT"}
    }
    assert unexpected == {}, [r for r in report["results"] if r["test_id"] in unexpected][:5]
    coverage = report["coverage"]
    assert coverage["component"] == {**coverage["component"], "covered": 11, "total": 12}
    assert coverage["system"] == {**coverage["system"], "covered": 17, "total": 18}
    design = json.loads(DESIGN.read_text("utf-8"))
    untested = sum(1 for item in design["requirements"] if not item["testIds"])
    assert coverage["model"]["total"] == len(design["requirements"])
    assert coverage["model"]["covered"] == len(design["requirements"]) - untested
    assert coverage["code"] == {"covered": 120, "total": 400, "basis": "Fake measured lines."}
    # The stored bytes round-trip unchanged with the profile retained.
    assert SuiteReport.model_validate_json(raw).model_dump_json().encode() == raw
    record = run_record(raw, manifest, tester_url="http://tester", target_revision="t")
    assert record["report_profile"] == "own" and record["counts"]["planned"] == 395


def test_full_cross_team_run_omits_the_model_key(tmp_path: Path) -> None:
    report, raw = run_phase5(
        tmp_path,
        FakePhase5(**SMALL),
        TARGET_TEAM_MAP_JSON={BASE_URL: "team12"},
        tester_design_map_path=None,
    )
    check_report_profile(report, "cross")
    assert b'"model"' not in raw
    result = outcomes(report)
    assert result["P5-TESTER-CROSS-OMIT"] == "pass"
    assert result["P5-TESTER-SELF-MODEL"] == "skip"
    assert Counter(result.values())["fail"] == 0 and Counter(result.values())["error"] == 0


# --- defective targets (P5-TESTER-EVALUABLE-FAIL, P5-TESTER-ERROR-SOURCE) ---------------------


@pytest.mark.parametrize(
    ("switch", "expected"),
    [
        ("reset_cadence_to_now", {"P5-SCHEDULE-COALESCE"}),
        ("remind_clean", {"P5-REMINDER-CLEAN", "P5-REMINDER-AUDIT-SAME-TICK"}),
        ("no_dedup", {"P5-REMINDER-DEDUP", "P5-REMINDER-CONFIG-EXACT", "P5-CLOCK-SAME"}),
        ("accept_interval_zero", {"P5-SCHEDULE-REJECT-ZERO"}),
        ("delete_keeps_schedule", {"P5-DELETE-COMPLETE", "P5-DELETE-OLD-URLS"}),
        (
            "reviewer_can_delete",
            {"P5-ACCESS-DELETE-GRANTED-REVIEWER", "P5-ACCESS-DELETE-UNGRANTED-REVIEWER"},
        ),
    ],
)
def test_defective_targets_fail_the_relevant_cases(
    tmp_path: Path, switch: str, expected: set[str]
) -> None:
    report, _ = run_phase5(tmp_path, FakePhase5(**SMALL, **{switch: True}))
    failed = {r["test_id"] for r in report["results"] if r["outcome"] == "fail"}
    assert expected <= failed
    assert not any(r["outcome"] == "error" for r in report["results"])


def test_incorrectly_accepted_clock_work_is_drained_without_masking_the_failure(
    tmp_path: Path,
) -> None:
    fake = FakePhase5(**SMALL, clock_ignores_busy=True)
    report, _ = run_phase5(tmp_path, fake)
    results = {r["test_id"]: r for r in report["results"]}
    for test_id in ("P5-SCHEDULE-BUSY-AUDIT", "P5-SCHEDULE-BUSY-REMEDIATION"):
        assert results[test_id]["outcome"] == "fail"
        assert "still active" in results[test_id]["message"]
    # The wrongly dispatched audits were polled to a terminal state through owner GETs only;
    # no mutation was replayed, and later independent cases were unaffected.
    assert not any(audit.status == "PROCESSING" for audit in fake.audits.values())
    assert all(
        r["outcome"] in ("pass", "skip")
        for r in report["results"]
        if r["test_id"] not in ("P5-SCHEDULE-BUSY-AUDIT", "P5-SCHEDULE-BUSY-REMEDIATION")
    )


def test_unexpected_scheduled_processing_failure_is_error_source_system(tmp_path: Path) -> None:
    report, _ = run_phase5(tmp_path, FakePhase5(**SMALL, scheduled_internal_failure=True))
    results = {r["test_id"]: r for r in report["results"]}
    exact = results["P5-SCHEDULE-EXACT-DUE"]
    assert (exact["outcome"], exact.get("source")) == ("error", "system")
    # The requested scheduled fault still matches its oracle and passes.
    assert results["P5-FAULT-SCHEDULED_AUDIT"]["outcome"] in ("pass", "error")
    assert not any(r["outcome"] == "fail" for r in report["results"])


def test_outcome_vectors_follow_the_instructor_rule() -> None:
    """P5-TESTER-ERROR-SOURCE: pass/fail/error-system/error-communication/skip mapping."""
    from tester.suite.checks import CheckFailed, Prerequisite, SystemFault
    from tester.target import CommunicationError

    suite = load_suite(MANIFEST, check_names())
    test = suite.manifest.tests[0].model_copy(update={"check": "probe"})

    class Ctx:
        current = None

    signals: list[tuple[BaseException | None, str, str | None]] = [
        (None, "pass", None),
        (CheckFailed("evaluable disagreement"), "fail", None),
        (SystemFault("unexpected internal failure"), "error", "system"),
        (CommunicationError("no response"), "error", "communication"),
        (Prerequisite("never ran"), "skip", None),
    ]
    for signal, outcome, source in signals:

        def probe(_: Any, signal: BaseException | None = signal) -> str:
            if signal is not None:
                raise signal
            return "expected controlled envelope matched"

        import tester.phase5.checks as phase5_checks

        phase5_checks.CHECKS["probe"] = probe
        try:
            result = runner._run_one_assertion(Ctx(), test)  # type: ignore[arg-type]
        finally:
            del phase5_checks.CHECKS["probe"]
        assert (result.outcome.value, result.source) == (outcome, source)


def test_unexpected_failure_keeps_phase4_rule_for_phase4_runs() -> None:
    from tester.phase4.checks import unexpected_failure
    from tester.suite.checks import CheckFailed, SystemFault

    class Ctx:
        def __init__(self, phase: str) -> None:
            manifest = "manifest-phase5.json" if phase == "5" else "manifest-phase4.json"
            self.suite = type(
                "S",
                (),
                {"manifest": SuiteManifest.model_validate_json((FIXTURES / manifest).read_bytes())},
            )

    assert isinstance(unexpected_failure(Ctx("5"), "x"), SystemFault)  # type: ignore[arg-type]
    assert isinstance(unexpected_failure(Ctx("4"), "x"), CheckFailed)  # type: ignore[arg-type]


def test_setup_skips_and_drains_earn_no_coverage(tmp_path: Path) -> None:
    """P5-TESTER-NO-SETUP-CREDIT: only executed qualifying assertions count."""
    suite = load_suite(MANIFEST, check_names())
    profile = classify(
        BASE_URL,
        tester_team="team9",
        target_teams={BASE_URL: "team9"},
        design_map_path=DESIGN,
        planned_ids=[t.test_id for t in suite.manifest.tests],
    )
    restored = type(suite)(suite.manifest, suite.expectations, suite.data, profile.snapshot())
    first = next(t for t in suite.manifest.tests if t.test_id == "P5-SCHEDULE-MIN")
    skipped = next(t for t in suite.manifest.tests if t.test_id == "P5-DELETE-COMPLETE")
    from app.contracts.models import TestResult

    results = [
        TestResult(test_id=first.test_id, outcome=Outcome.FAIL, message="evaluated"),
        TestResult(test_id=skipped.test_id, outcome=Outcome.SKIP, message="never ran"),
    ]
    markers = {
        first.test_id: {"components": ["C10"], "acceptance": ["AC-12"]},
        # A marker recorded before a skip earns nothing.
        skipped.test_id: {"components": ["C12", "C3"], "acceptance": ["AC-16"]},
    }
    code = CoverageMeasure(covered=None, total=None, basis="Target code coverage unavailable")
    report = runner.build_report(restored, "s", BASE_URL, results, code, markers)
    assert (report.coverage.component.covered, report.coverage.system.covered) == (1, 1)
    assert report.coverage.model is not None
    mapped = sum(1 for item in profile.requirements if first.test_id in item["testIds"])
    assert report.coverage.model.covered == mapped


def test_unavailable_code_coverage_is_null_not_zero(tmp_path: Path) -> None:
    fake = FakePhase5(**SMALL)
    original = fake.instructor

    def refuse_report(request: Any, path: str) -> Any:
        if path == "/test/report":
            import httpx

            return httpx.Response(503, json={"message": "unavailable", "source": "system"})
        return original(request, path)

    fake.instructor = refuse_report  # type: ignore[method-assign]
    report, _ = run_phase5(tmp_path, fake)
    code = report["coverage"]["code"]
    assert (code["covered"], code["total"]) == (None, None) and "unavailable" in code["basis"]


def test_abort_and_recovery_keep_the_frozen_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P5-TESTER-ABORT and P5-TESTER-ONE-RUN on the Phase 5 profile."""
    calls = {"n": 0}
    original = runner.run_one

    def explode(ctx: Any, test: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("tester defect")
        return original(ctx, test)

    monkeypatch.setattr(runner, "run_one", explode)
    with client_for(tmp_path, FakePhase5(**SMALL)) as client:
        sid = client.post("/test/setup", json={"target_url": BASE_URL}, headers=HEADERS).json()[
            "session_id"
        ]
        run = client.post("/test/run", json={"session_id": sid}, headers=HEADERS)
        assert run.status_code == 500 and run.json()["source"] == "tester"
        stored = client.get("/test/report", params={"session_id": sid}, headers=HEADERS)
        assert (
            client.post("/test/run", json={"session_id": sid}, headers=HEADERS).status_code == 409
        )
    report = stored.json()
    check_report_profile(report, "own")
    counts = Counter(r["outcome"] for r in report["results"])
    assert (counts["pass"], counts["error"], counts["skip"]) == (2, 1, len(report["results"]) - 3)
    errored = next(r for r in report["results"] if r["outcome"] == "error")
    assert errored["source"] == "tester"
    assert report["coverage"]["code"]["covered"] is None

    # A tester process that stopped mid-run is finalized on restart with the same profile.
    monkeypatch.setattr(runner, "run_one", original)
    with client_for(tmp_path / "second", FakePhase5(**SMALL)) as client:
        sid = client.post("/test/setup", json={"target_url": BASE_URL}, headers=HEADERS).json()[
            "session_id"
        ]
        store = client.app.state.tester.store  # type: ignore[attr-defined]
        store.begin_run(sid, "op")
    with client_for(tmp_path / "second", FakePhase5(**SMALL)) as client:
        recovered = client.get("/test/report", params={"session_id": sid}, headers=HEADERS)
    assert recovered.status_code == 200
    check_report_profile(recovered.json(), "own")


def test_system_setup_errors_are_forwarded_unchanged(tmp_path: Path) -> None:
    """P5-TESTER-SETUP-FORWARD: the system's status/message/source pass through."""
    fake = FakePhase5(**SMALL)
    original = fake.instructor

    def refuse(request: Any, path: str) -> Any:
        if path == "/test/setup":
            import httpx

            return httpx.Response(
                503, json={"message": "Processing resources are stopped.", "source": "system"}
            )
        return original(request, path)

    fake.instructor = refuse  # type: ignore[method-assign]
    with client_for(tmp_path, fake) as client:
        response = client.post("/test/setup", json={"target_url": BASE_URL}, headers=HEADERS)
    assert response.status_code == 503
    assert response.json() == {"message": "Processing resources are stopped.", "source": "system"}


def test_target_client_never_resends_put_or_delete(tmp_path: Path) -> None:
    import httpx

    from app.config import AppEnvironment
    from tester.config import TargetAuth
    from tester.target import CommunicationError, TargetClient

    attempts: Counter[str] = Counter()

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts[request.method] += 1
        raise httpx.ConnectError("reset")

    client = TargetClient(
        BASE_URL,
        TargetAuth.model_validate(access_map()[BASE_URL]),
        AppEnvironment.TEST,
        10_000_000_000.0,
        transport=httpx.MockTransport(flaky),
        resolver=address_resolver(),
    )
    for method in ("GET", "PUT", "DELETE"):
        with pytest.raises(CommunicationError):
            client.request(method, "/api/v1/workbooks/w/schedule", identity="userA")  # type: ignore[arg-type]
    assert attempts == {"GET": 4, "PUT": 1, "DELETE": 1}


def test_coverage_catalogs_are_current_and_reference_real_tests() -> None:
    import inspect

    from scripts.build_phase5_catalogs import OUTPUT, TESTER_LOCAL
    from scripts.build_phase5_catalogs import build as build_catalogs

    assert OUTPUT.read_text("utf-8") == build_catalogs()
    import sys

    here = {name for name, _ in inspect.getmembers(sys.modules[__name__], inspect.isfunction)}
    for names in TESTER_LOCAL.values():
        assert set(names) <= here


def test_rename_probe_variants_isolate_one_reference_kind() -> None:
    from tester.phase4.package import compare_packages
    from tester.phase5.rename_probe import plan, variants

    built = variants()
    assert len({v.name for v in built}) == len(built) == 10
    assert all(len(v.references) <= 1 for v in built)
    assert [v.name for v in built if v.interpretation_question] == ["conditional_x14"]
    for variant in built:
        assert variant.data == next(v.data for v in variants() if v.name == variant.name)
        # An output that never applied the rename is reported, not accepted.
        assert compare_packages(variant.data, variant.data, [plan(variant)])


def test_fast_target_reports_unobservable_windows_as_clean_skips(tmp_path: Path) -> None:
    """Work that finished before the clock/DELETE call yields a plain skip, never a drain error."""
    fast = FakePhase5(**SMALL, processing_polls=0, remediation_polls=0, large_polls=0)
    report, _ = run_phase5(tmp_path, fast)
    assert not [r for r in report["results"] if r["outcome"] in ("fail", "error")]
    assert not [r for r in report["results"] if "Background acceptance" in r["message"]]
    skipped = {r["test_id"] for r in report["results"] if r["outcome"] == "skip"}
    assert {"P5-SCHEDULE-BUSY-AUDIT", "P5-DELETE-ACTIVE-AUDIT"} <= skipped
