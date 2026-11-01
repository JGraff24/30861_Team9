"""Tester service behavior against a test-only fake target over an injected HTTP transport."""

import hashlib
import json
import logging
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import AppEnvironment
from app.contracts.models import Outcome, SuiteReport, TestResult
from scripts.make_synthetic_fixtures import FIXTURE_DIR, fixtures
from tester.config import TesterSettings as SettingsForTester
from tester.main import TesterServices as ServicesForTester
from tester.main import create_app
from tester.store import TesterSessionState as SessionState
from tester.store import TesterStore as Store
from tester.suite import checks
from tester.suite.checks import CHECKS
from tester.suite.identity import finding_id
from tester.suite.manifest import SuiteConfigurationError, SuiteManifest, load_suite, snapshot_suite
from tester.suite.records import run_record
from tester.target import CommunicationError, check_address
from tests.support.fake_system import BASE_URL, TOKENS, FakeSystem, access_map, address_resolver

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "tests" / "fixtures" / "manifest.json"
OPERATOR = "grader-operator-token"
OTHER_OPERATOR = "other-operator-token"
AUTH = {"Authorization": f"Bearer {OPERATOR}"}
OTHER = {"Authorization": f"Bearer {OTHER_OPERATOR}"}
SMALL_LIMIT = 60_000
SMALL_SHEETS = 5
SMALL_CELLS = 100


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name, item in SettingsForTester.model_fields.items():
        monkeypatch.delenv(str(item.validation_alias or name.upper()), raising=False)
    monkeypatch.chdir(tmp_path)
    # Keep the boundary checks fast; the real limit is exercised by the live suite.
    monkeypatch.setattr(checks, "UPLOAD_LIMIT_BYTES", SMALL_LIMIT)
    monkeypatch.setattr(checks, "WORKSHEET_LIMIT", SMALL_SHEETS)
    monkeypatch.setattr(checks, "NONEMPTY_CELL_LIMIT", SMALL_CELLS)


def small_fake(**options: Any) -> FakeSystem:
    return FakeSystem(
        upload_limit=SMALL_LIMIT, worksheet_limit=SMALL_SHEETS, cell_limit=SMALL_CELLS, **options
    )


def settings(tmp_path: Path, **overrides: Any) -> SettingsForTester:
    values: dict[str, Any] = {
        "tester_db_path": tmp_path / "tester.sqlite3",
        "allowed_target_urls": [BASE_URL],
        "TARGET_AUTH_MAP_JSON": access_map(),
        "TESTER_OPERATOR_TOKEN_HASHES_JSON": {
            digest(OPERATOR): "grader",
            digest(OTHER_OPERATOR): "other",
        },
        "fixture_manifest_path": MANIFEST,
    } | overrides
    return SettingsForTester(**values)


@pytest.fixture
def make_client(tmp_path: Path) -> Iterator[Callable[..., tuple[TestClient, FakeSystem]]]:
    clients: list[TestClient] = []

    def build(
        fake: FakeSystem | None = None, clock: FakeClock | None = None, **overrides: Any
    ) -> tuple[TestClient, FakeSystem]:
        fake = fake or small_fake()
        clock = clock or FakeClock()
        services = ServicesForTester(
            settings=settings(tmp_path, **overrides),
            transport=fake.transport(),
            resolver=address_resolver(),
            sleep=clock.sleep,
            clock=clock,
        )
        client = TestClient(create_app(services=services), raise_server_exceptions=False)
        client.__enter__()
        clients.append(client)
        return client, fake

    yield build
    for client in clients:
        client.__exit__(None, None, None)


def setup_session(client: TestClient) -> str:
    response = client.post("/test/setup", json={"target_url": BASE_URL}, headers=AUTH)
    assert response.status_code == 201, response.text
    session_id: str = response.json()["session_id"]
    return session_id


def outcomes(report: SuiteReport) -> dict[str, tuple[str, str | None]]:
    return {
        r.test_id: (r.outcome.value, r.source.value if r.source else None) for r in report.results
    }


def test_full_run_against_conforming_target(make_client: Callable[..., Any]) -> None:
    client, fake = make_client()
    session_id = setup_session(client)
    assert session_id in fake.sessions
    run = client.post("/test/run", json={"session_id": session_id}, headers=AUTH)
    assert run.status_code == 200, run.text
    report = SuiteReport.model_validate_json(run.content)
    planned = [test["test_id"] for test in json.loads(MANIFEST.read_text())["tests"]]
    assert [r.test_id for r in report.results] == planned
    assert all(
        r.outcome == Outcome.PASS
        for r in report.results
        if r.test_id != "P3-AUDIT-FAILURE-ENVELOPE"
    ), outcomes(report)
    assert outcomes(report)["P3-AUDIT-FAILURE-ENVELOPE"] == ("skip", None)
    assert (report.coverage.component.covered, report.coverage.component.total) == (6, 6)
    assert (report.coverage.system.covered, report.coverage.system.total) == (7, 8)
    assert (report.coverage.code.covered, report.coverage.code.total) == (40, 50)
    # Every project call carries a session header; only P3-SESSION-UNKNOWN names another.
    project = [r for r in fake.requests if r.url.path.startswith("/api/")]
    headers = [r.headers.get("x-test-session") for r in project]
    assert None not in headers and len([h for h in headers if h != session_id]) == 1
    assert all(
        "x-test-session" not in r.headers for r in fake.requests if r.url.path.startswith("/test/")
    )
    stored = client.get("/test/report", params={"session_id": session_id}, headers=AUTH)
    assert stored.status_code == 200 and stored.content == run.content
    again = client.post("/test/run", json={"session_id": session_id}, headers=AUTH)
    assert again.status_code == 409 and again.json()["source"] == "tester"


def test_credentials_never_reach_report_or_logs(
    make_client: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    client, _ = make_client()
    session_id = setup_session(client)
    run = client.post("/test/run", json={"session_id": session_id}, headers=AUTH)
    for secret in [*TOKENS.values(), OPERATOR]:
        assert secret not in run.text
        assert secret not in caplog.text


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/test/setup", {"target_url": BASE_URL}),
        ("post", "/test/run", {"session_id": "abc"}),
        ("get", "/test/report?session_id=abc", None),
    ],
)
def test_operator_authentication_is_required(
    make_client: Callable[..., Any], method: str, path: str, body: Any
) -> None:
    client, fake = make_client()
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic x"}):
        response = getattr(client, method)(
            path, headers=headers, **({"json": body} if body else {})
        )
        assert response.status_code == 401
        assert response.json() == {
            "message": "Missing or invalid operator credential.",
            "source": "tester",
        }
    assert fake.requests == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"target_url": ""},
        {"target_url": BASE_URL, "extra": 1},
        {"target_url": "http://127.0.0.1:9999"},
        {"target_url": BASE_URL + "/"},
    ],
)
def test_setup_rejects_invalid_or_unapproved_targets(
    make_client: Callable[..., Any], body: dict[str, Any]
) -> None:
    client, fake = make_client()
    response = client.post("/test/setup", json=body, headers=AUTH)
    assert response.status_code == 400 and response.json()["source"] == "tester"
    assert fake.requests == []


def test_setup_forwards_system_errors_and_leaves_no_runnable_session(
    make_client: Callable[..., Any], tmp_path: Path
) -> None:
    fake = FakeSystem(
        setup_response=lambda body: httpx.Response(
            503, json={"message": "preparation failed", "source": "system"}
        )
    )
    client, _ = make_client(fake)
    response = client.post("/test/setup", json={"target_url": BASE_URL}, headers=AUTH)
    assert response.status_code == 503
    assert response.json() == {"message": "preparation failed", "source": "system"}
    store = Store(tmp_path / "tester.sqlite3")
    failed = store.cleanup_candidates(BASE_URL)
    assert len(failed) == 1
    session = store.get(failed[0])
    assert session is not None and session.state == SessionState.SETUP_FAILED
    run = client.post("/test/run", json={"session_id": failed[0]}, headers=AUTH)
    assert run.status_code == 409


@pytest.mark.parametrize(
    ("fake", "status", "source"),
    [
        (FakeSystem(drop_paths={"/test/setup"}), 500, "communication"),
        (
            FakeSystem(setup_response=lambda b: httpx.Response(201, json={"session_id": "other"})),
            500,
            "system",
        ),
        (FakeSystem(setup_response=lambda b: httpx.Response(500, text="boom")), 500, "system"),
        (FakeSystem(setup_response=lambda b: httpx.Response(200, json=b)), 500, "system"),
    ],
)
def test_setup_failure_sources(
    make_client: Callable[..., Any], fake: FakeSystem, status: int, source: str
) -> None:
    client, _ = make_client(fake)
    response = client.post("/test/setup", json={"target_url": BASE_URL}, headers=AUTH)
    assert response.status_code == status and response.json()["source"] == source


def test_failed_setup_cleanup_is_automatic(make_client: Callable[..., Any]) -> None:
    failing = FakeSystem(
        setup_response=lambda body: httpx.Response(500, json={"message": "x", "source": "system"})
    )
    client, _ = make_client(failing)
    client.post("/test/setup", json={"target_url": BASE_URL}, headers=AUTH)
    seen: list[dict[str, Any]] = []
    recording = FakeSystem(
        setup_response=lambda body: (
            seen.append(body),
            httpx.Response(201, json={"session_id": body["session_id"]}),
        )[1]
    )
    enabled, _ = make_client(recording)
    setup_session(enabled)
    assert len(seen[0]["setup"]["cleanup_session_ids"]) == 1
    setup_session(enabled)
    assert seen[1]["setup"] == {}


def test_report_access_rules(make_client: Callable[..., Any]) -> None:
    client, _ = make_client()
    session_id = setup_session(client)
    params = {"session_id": session_id}
    assert client.get("/test/report", params=params, headers=AUTH).status_code == 409
    assert client.get("/test/report", params=params, headers=OTHER).status_code == 403
    assert (
        client.get("/test/report", params={"session_id": "unknown"}, headers=AUTH).status_code
        == 404
    )
    assert client.get("/test/report", headers=AUTH).status_code == 400
    assert (
        client.get("/test/report", params={"session_id": "bad id!"}, headers=AUTH).status_code
        == 400
    )
    assert (
        client.post("/test/run", json={"session_id": session_id}, headers=OTHER).status_code == 403
    )
    assert client.post("/test/run", json={"session_id": "unknown"}, headers=AUTH).status_code == 404
    assert client.post("/test/run", json={}, headers=AUTH).status_code == 400


def test_concurrent_run_is_rejected(make_client: Callable[..., Any]) -> None:
    gate = threading.Event()
    client, fake = make_client(small_fake(gate=gate))
    session_id = setup_session(client)
    first: dict[str, httpx.Response] = {}
    worker = threading.Thread(
        target=lambda: first.setdefault(
            "response", client.post("/test/run", json={"session_id": session_id}, headers=AUTH)
        )
    )
    worker.start()
    while not any(r.url.path == "/api/v1/health" for r in fake.requests):
        threading.Event().wait(0.01)
    second = client.post("/test/run", json={"session_id": session_id}, headers=AUTH)
    gate.set()
    worker.join(timeout=30)
    assert second.status_code == 409
    assert first["response"].status_code == 200


def test_target_failures_map_to_outcomes(make_client: Callable[..., Any]) -> None:
    def wrong_classification(findings: list[dict[str, str]]) -> list[dict[str, str]]:
        return [f | {"classification": "MANUAL_REVIEW"} for f in findings]

    fake = FakeSystem(
        upload_limit=SMALL_LIMIT,
        status_overrides={"/api/v1/health": 500},
        drop_paths=set(),
        mutate_findings=wrong_classification,
    )
    client, _ = make_client(fake)
    session_id = setup_session(client)
    report = SuiteReport.model_validate_json(
        client.post("/test/run", json={"session_id": session_id}, headers=AUTH).content
    )
    results = outcomes(report)
    assert results["P3-HEALTH"] == ("error", "system")
    assert results["P3-AUDIT-SEEDED"] == ("fail", None)
    assert all(
        results[test_id] == ("pass", None)
        for test_id in (
            "P3-DETERMINISM",
            "P3-DETERMINISM-3",
            "P3-DETERMINISM-4",
            "P3-DETERMINISM-5",
        )
    )
    # Consistently wrong findings still exercise repeatability; AC-14 was not observed.
    assert report.coverage.system.covered == 7


def test_communication_failure_is_reported_per_test(make_client: Callable[..., Any]) -> None:
    client, fake = make_client()
    session_id = setup_session(client)
    fake.drop_paths.add("/api/v1/health")
    report = SuiteReport.model_validate_json(
        client.post("/test/run", json={"session_id": session_id}, headers=AUTH).content
    )
    assert outcomes(report)["P3-HEALTH"] == ("error", "communication")
    health_calls = [r for r in fake.requests if r.url.path == "/api/v1/health"]
    assert len(health_calls) == 4  # one try plus three GET transport retries


def test_post_is_never_retried(make_client: Callable[..., Any]) -> None:
    client, fake = make_client()
    session_id = setup_session(client)
    fake.drop_paths.add("/api/v1/workbooks")
    client.post("/test/run", json={"session_id": session_id}, headers=AUTH)
    uploads = [r for r in fake.requests if r.url.path == "/api/v1/workbooks"]
    # Original 16 + ten extra fixtures + missing extension + five pod contract 1.1 fixtures
    # + 50 worksheets with a chartsheet; determinism skips.
    planned_uploads = 33
    assert len(uploads) == planned_uploads


def test_audit_that_never_finishes_fails_within_deadline(make_client: Callable[..., Any]) -> None:
    # Ten polled audits x 75 s exceed the 600 s suite budget; isolate the per-audit deadline.
    client, _ = make_client(small_fake(processing_polls=10**6), tester_run_timeout_seconds=1200)
    session_id = setup_session(client)
    report = SuiteReport.model_validate_json(
        client.post("/test/run", json={"session_id": session_id}, headers=AUTH).content
    )
    results = outcomes(report)
    assert results["P3-AUDIT-CLEAN"] == ("fail", None)
    assert "PROCESSING after 75 s" in next(
        r.message for r in report.results if r.test_id == "P3-AUDIT-CLEAN"
    )


def test_redirects_are_not_followed(make_client: Callable[..., Any]) -> None:
    fake = small_fake()
    original = fake.handle

    def redirecting(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/health":
            fake.requests.append(request)
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"})
        return original(request)

    fake.handle = redirecting  # type: ignore[method-assign]
    client, _ = make_client(fake)
    session_id = setup_session(client)
    report = SuiteReport.model_validate_json(
        client.post("/test/run", json={"session_id": session_id}, headers=AUTH).content
    )
    assert outcomes(report)["P3-HEALTH"] == ("fail", None)
    assert all(r.url.host != "169.254.169.254" for r in fake.requests)


def test_deadline_abort_stores_report_and_returns_500(make_client: Callable[..., Any]) -> None:
    clock = FakeClock()
    fake = small_fake()
    original = fake.handle

    def slow_health(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/health":
            clock.now += 601  # the suite budget is spent while P3-HEALTH is answered
        return original(request)

    fake.handle = slow_health  # type: ignore[method-assign]
    client, _ = make_client(fake, clock)
    session_id = setup_session(client)
    run = client.post("/test/run", json={"session_id": session_id}, headers=AUTH)
    assert run.status_code == 500 and run.json()["source"] == "tester"
    stored = client.get("/test/report", params={"session_id": session_id}, headers=AUTH)
    report = SuiteReport.model_validate_json(stored.content)
    assert (report.results[0].outcome, report.results[0].source) == (Outcome.ERROR, "tester")
    assert {r.outcome for r in report.results[1:]} == {Outcome.SKIP}
    assert report.coverage.code.covered is None and report.coverage.code.total is None
    again = client.post("/test/run", json={"session_id": session_id}, headers=AUTH)
    assert again.status_code == 409


def test_invalid_suite_configuration_does_not_consume_session(
    make_client: Callable[..., Any], tmp_path: Path
) -> None:
    client, fake = make_client()
    session_id = setup_session(client)
    # A snapshotted fixture that changes before run must fail before consumption.
    store = client.app.state.tester.store
    with store._connect(write=True) as db:
        row = db.execute(
            "SELECT plan FROM tester_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        plan = json.loads(row[0])
        plan["fixture_root"] = str(tmp_path / "missing-fixtures")
        db.execute(
            "UPDATE tester_sessions SET plan = ? WHERE session_id = ?",
            (json.dumps(plan), session_id),
        )
    run = client.post("/test/run", json={"session_id": session_id}, headers=AUTH)
    assert run.status_code == 500 and run.json()["source"] == "tester"
    session = Store(tmp_path / "tester.sqlite3").get(session_id)
    assert session is not None and session.state == SessionState.READY
    assert len(fake.requests) == 1


def test_interrupted_run_is_recovered_as_aborted(tmp_path: Path) -> None:
    store = Store(tmp_path / "tester.sqlite3")
    store.create(
        "crashed-session",
        "grader",
        BASE_URL,
        datetime.now(UTC),
        plan=snapshot_suite(load_suite(MANIFEST, set(CHECKS)), MANIFEST),
    )
    store.finish_setup("crashed-session", ready=True)
    store.begin_run("crashed-session", "grader")
    done = TestResult(test_id="P3-HEALTH", outcome=Outcome.PASS, message="Health matched.")
    store.save_progress("crashed-session", [done])
    services = ServicesForTester(settings=settings(tmp_path))
    with TestClient(create_app(services=services)) as client:
        response = client.get(
            "/test/report", params={"session_id": "crashed-session"}, headers=AUTH
        )
    report = SuiteReport.model_validate_json(response.content)
    assert report.results[0] == done
    assert (report.results[1].outcome, report.results[1].source) == (Outcome.ERROR, "tester")
    assert {r.outcome for r in report.results[2:]} == {Outcome.SKIP}


def test_store_consumes_a_session_exactly_once(tmp_path: Path) -> None:
    store = Store(tmp_path / "tester.sqlite3")
    store.create("s1", "grader", BASE_URL, datetime.now(UTC))
    store.finish_setup("s1", ready=True)
    wins: list[str] = []
    failures: list[int] = []

    def attempt() -> None:
        try:
            store.begin_run("s1", "grader")
            wins.append("won")
        except Exception as exc:
            failures.append(getattr(exc, "status_code", 0))

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert wins == ["won"] and failures == [409] * 7
    store.finish_run("s1", b"{}", aborted=False)
    with pytest.raises(RuntimeError):
        store.finish_run("s1", b"{}", aborted=False)


@pytest.mark.parametrize(
    ("address", "environment", "allowed"),
    [
        ("127.0.0.1", AppEnvironment.DEVELOPMENT, True),
        ("127.0.0.1", AppEnvironment.PRODUCTION, False),
        ("10.0.0.5", AppEnvironment.PRODUCTION, False),
        ("93.184.216.34", AppEnvironment.PRODUCTION, True),
        ("169.254.169.254", AppEnvironment.DEVELOPMENT, False),
        ("fd00:ec2::254", AppEnvironment.DEVELOPMENT, False),
        ("0.0.0.0", AppEnvironment.DEVELOPMENT, False),
        ("224.0.0.1", AppEnvironment.DEVELOPMENT, False),
        ("fe80::1", AppEnvironment.DEVELOPMENT, False),
    ],
)
def test_resolved_address_policy(address: str, environment: AppEnvironment, allowed: bool) -> None:
    resolver = address_resolver(address)
    if allowed:
        check_address("target.example", environment, resolver)
    else:
        with pytest.raises(CommunicationError):
            check_address("target.example", environment, resolver)


def test_unresolvable_target_is_a_communication_error() -> None:
    def fail(*args: object, **kwargs: object) -> list[Any]:
        raise OSError("no such host")

    with pytest.raises(CommunicationError):
        check_address("missing.example", AppEnvironment.DEVELOPMENT, fail)


def test_manifest_and_expectations_are_valid_and_independent() -> None:
    suite = load_suite(MANIFEST, set(CHECKS))
    assert {test.check for test in suite.manifest.tests} == set(CHECKS)
    declared = {c for test in suite.manifest.tests for c in test.components}
    assert declared == set(suite.manifest.components)
    declared_acs = {a for test in suite.manifest.tests for a in test.acceptance}
    assert declared_acs == set(suite.manifest.acceptance)
    seeded = suite.accepted("seeded_issues")
    keys = [(f.ruleId, f.worksheet, f.target) for f in seeded.findings]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)


def test_tampered_fixture_is_refused(tmp_path: Path) -> None:
    (tmp_path / "synthetic").mkdir()
    for name in ("clean.xlsx", "seeded_issues.xlsx", "corrupt.xlsx"):
        (tmp_path / "synthetic" / name).write_bytes((FIXTURE_DIR / name).read_bytes())
    (tmp_path / "synthetic" / "corrupt.xlsx").write_bytes(b"changed")
    for name in ("manifest.json", "expectations.json"):
        (tmp_path / name).write_bytes((MANIFEST.parent / name).read_bytes())
    with pytest.raises(SuiteConfigurationError, match="SHA256"):
        load_suite(tmp_path / "manifest.json", set(CHECKS))
    with pytest.raises(SuiteConfigurationError, match="unknown checks"):
        load_suite(MANIFEST, {"health"})


def test_committed_synthetic_fixtures_match_generator() -> None:
    for name, data in fixtures().items():
        assert (FIXTURE_DIR / name).read_bytes() == data


def test_padded_workbook_is_exact_size() -> None:
    from tester.suite.workbooks import padded_clean_workbook

    assert len(padded_clean_workbook(SMALL_LIMIT)) == SMALL_LIMIT


def test_finding_id_matches_contract_vectors() -> None:
    golden = ["0" * 64, "1.0.0", "ALT_TEXT", "Expenses", "xl/drawings/drawing1.xml#id=7"]
    assert finding_id(*golden) == (
        "502d6624551c9da5c9a80d7efcc04be1119dad4a16f55171487949600a177714"
    )
    composed = finding_id("0" * 64, "1.0.0", "WORKSHEET_NAME", "Dépenses \U0001f4ca", "t")
    decomposed = finding_id("0" * 64, "1.0.0", "WORKSHEET_NAME", "Dépenses \U0001f4ca", "t")
    assert composed != decomposed


def run_once(client: TestClient) -> httpx.Response:
    session_id = setup_session(client)
    response: httpx.Response = client.post(
        "/test/run", json={"session_id": session_id}, headers=AUTH
    )
    return response


def test_unavailable_target_instrumentation_is_paired_null(
    make_client: Callable[..., Any],
) -> None:
    basis = "Code measurement disabled on this deployment."
    fake = small_fake(code_coverage={"covered": None, "total": None, "basis": basis})
    client, _ = make_client(fake)
    report = SuiteReport.model_validate_json(run_once(client).content)
    assert (report.coverage.code.covered, report.coverage.code.total) == (None, None)
    assert report.coverage.code.basis == basis
    assert outcomes(report)["P3-CODE-REPORT"] == ("pass", None)


def test_failed_coverage_retrieval_is_reported_not_invented(
    make_client: Callable[..., Any],
) -> None:
    client, _ = make_client(small_fake(status_overrides={"/test/report": 503}))
    report = SuiteReport.model_validate_json(run_once(client).content)
    assert (report.coverage.code.covered, report.coverage.code.total) == (None, None)
    assert "HTTP 503" in report.coverage.code.basis
    assert outcomes(report)["P3-CODE-REPORT"] == ("error", "system")
    # Earlier outcomes are kept even though coverage retrieval failed.
    assert outcomes(report)["P3-AUDIT-SEEDED"] == ("pass", None)


def test_run_record_reports_all_operations_without_secrets(
    make_client: Callable[..., Any],
) -> None:
    def wrong_classification(findings: list[dict[str, str]]) -> list[dict[str, str]]:
        swapped = {"MANUAL_REVIEW": "REQUIRES_USER_INPUT", "REQUIRES_USER_INPUT": "MANUAL_REVIEW"}
        return [f | {"classification": swapped[f["classification"]]} for f in findings]

    client, _ = make_client(small_fake(mutate_findings=wrong_classification))
    body = run_once(client).content
    manifest = SuiteManifest.model_validate_json(MANIFEST.read_bytes())
    record = run_record(
        body,
        manifest,
        tester_url="http://127.0.0.1:8001",
        target_revision="unknown",
        recorded_at=datetime(2026, 10, 14, 18, 0, tzinfo=UTC),
    )
    assert record["recorded_at"] == "2026-10-14T18:00:00Z"
    assert record["report_sha256"] == hashlib.sha256(body).hexdigest()
    assert record["counts"]["planned"] == len(manifest.tests)
    assert record["counts"]["run"] + record["counts"]["skip"] == len(manifest.tests)
    assert record["counts"]["fail"] == 8 and record["counts"]["skip"] == 1
    assert {item["test_id"] for item in record["not_passed"]} == {
        "P3-AUDIT-SEEDED",
        "P3-AUDIT-UNICODE",
        "P3-AUDIT-REPEATED-IDS",
        "P3-AUDIT-MULTIPLE-DRAWINGS",
        "P3-AUDIT-IN-CELL-PICTURE",
        "P3-AUDIT-IMAGE-FORMULA",
        "P3-AUDIT-FORM-CONTROLS",
        "P3-AUDIT-VML-COMMENTS",
        "P3-AUDIT-FAILURE-ENVELOPE",
    }
    assert all(item["classification"] == "unclassified" for item in record["not_passed"])
    status = {row["number"]: row for row in record["operation_status"]}
    assert sorted(status) == list(range(1, 15))
    assert status[1]["success_conformance"] == "yes"
    assert status[2]["error_conformance"] == "yes"
    assert status[5]["success_conformance"] == "no"  # seeded findings differed
    assert status[5]["error_conformance"] == "incomplete"
    assert status[4]["implemented"] == "observed responding"
    assert status[3]["implemented"] == "not delivered in Phase 3"
    assert status[3]["success_conformance"] == "untested"
    assert status[3]["method"] == "POST"
    assert status[3]["path"] == "/api/v1/workbooks/{workbookId}/versions"
    assert status[1]["requirements"] == ["REQ-15"]
    serialized = json.dumps(record)
    for secret in [*TOKENS.values(), OPERATOR]:
        assert secret not in serialized


def test_limit_and_protection_workbooks() -> None:
    import io
    import re
    import zipfile

    from tester.suite.workbooks import (
        cells_workbook,
        inactive_protection_workbook,
        protected_workbook,
        worksheets_workbook,
    )

    def parts(data: bytes) -> dict[str, bytes]:
        package = zipfile.ZipFile(io.BytesIO(data))
        return {name: package.read(name) for name in package.namelist()}

    protected = parts(protected_workbook())
    assert b"workbookProtection" in protected["xl/workbook.xml"]
    assert b"sheetProtection" in protected["xl/worksheets/sheet1.xml"]
    assert parts(worksheets_workbook(51))["xl/workbook.xml"].count(b"<sheet ") == 51
    with_chart = parts(worksheets_workbook(50, chartsheet=True))
    assert with_chart["xl/workbook.xml"].count(b"<sheet ") == 51
    assert sum(name.startswith("xl/worksheets/sheet") for name in with_chart) == 50
    assert with_chart["xl/chartsheets/sheet1.xml"].startswith(b"<?xml")
    inactive = parts(inactive_protection_workbook())["xl/worksheets/sheet1.xml"]
    assert b"sheetProtection" in inactive and b'sheet="0"' in inactive
    sheet = parts(cells_workbook(250_001))["xl/worksheets/sheet1.xml"].decode()
    assert len(re.findall(r"<c r=", sheet)) == 250_001


@pytest.mark.parametrize("field", ["workbookId", "versionId", "auditId"])
def test_audit_response_must_identify_requested_resource(
    make_client: Callable[..., Any], field: str
) -> None:
    fake = small_fake()
    original = fake.read

    def wrong(path: str, subject: str) -> httpx.Response:
        response = original(path, subject)
        if response.status_code == 200 and "/audits/" in path:
            return httpx.Response(200, json=response.json() | {field: "wrong-resource"})
        return response

    fake.read = wrong  # type: ignore[method-assign]
    client, _ = make_client(fake)
    report = SuiteReport.model_validate_json(run_once(client).content)
    assert outcomes(report)["P3-AUDIT-CLEAN"] == ("fail", None)
    assert outcomes(report)["P3-AUDIT-SEEDED"] == ("fail", None)
    assert outcomes(report)["P3-DETERMINISM"] == ("skip", None)


def test_determinism_checks_prose_and_continues_all_five_uploads(
    make_client: Callable[..., Any],
) -> None:
    fake = small_fake()
    original = fake.read

    def changing(path: str, subject: str) -> httpx.Response:
        response = original(path, subject)
        if response.status_code == 200 and "/audits/" in path:
            data = response.json()
            for finding in data["findings"]:
                finding["recommendation"] = "Review finding from " + data["auditId"]
            return httpx.Response(200, json=data)
        return response

    fake.read = changing  # type: ignore[method-assign]
    client, _ = make_client(fake)
    report = SuiteReport.model_validate_json(run_once(client).content)
    repeats = [r for r in report.results if r.test_id.startswith("P3-DETERMINISM")]
    assert len(repeats) == 4 and all(r.outcome == Outcome.FAIL for r in repeats)
    assert len([a for a in fake.audits.values() if a.file_name == "seeded_issues.xlsx"]) == 5


def test_failed_hash_request_earns_no_hash_coverage(make_client: Callable[..., Any]) -> None:
    fake = small_fake()
    original = fake.read

    def missing_version(path: str, subject: str) -> httpx.Response:
        response = original(path, subject)
        if (
            response.status_code == 200
            and "/audits/" not in path
            and response.json().get("fileName") == "seeded_issues.xlsx"
        ):
            return fake.project_error(404, "NOT_FOUND")
        return response

    fake.read = missing_version  # type: ignore[method-assign]
    client, _ = make_client(fake)
    report = SuiteReport.model_validate_json(run_once(client).content)
    assert outcomes(report)["P3-SOURCE-HASH"] == ("fail", None)
    assert report.coverage.system.covered == 6  # AC-03 and unobserved AC-14 excluded


def test_coverage_is_retrieved_once_inside_declared_test(make_client: Callable[..., Any]) -> None:
    client, fake = make_client()
    report = SuiteReport.model_validate_json(run_once(client).content)
    assert outcomes(report)["P3-CODE-REPORT"] == ("pass", None)
    assert len([r for r in fake.requests if r.url.path == "/test/report"]) == 1
    assert report.coverage.code.covered == 40


@pytest.mark.parametrize("status", [500, 503])
def test_health_and_upload_faults_are_system_errors(
    make_client: Callable[..., Any], status: int
) -> None:
    client, _ = make_client(
        small_fake(status_overrides={"/api/v1/health": status, "/api/v1/workbooks": status})
    )
    report = SuiteReport.model_validate_json(run_once(client).content)
    assert outcomes(report)["P3-HEALTH"] == ("error", "system")
    assert outcomes(report)["P3-UPLOAD-CLEAN"] == ("error", "system")
    assert outcomes(report)["P3-AUDIT-CLEAN"] == ("skip", None)
    assert report.coverage.system.covered == 0


def test_processing_failure_is_a_system_error_with_separate_failure_assertion(
    make_client: Callable[..., Any],
) -> None:
    fake = small_fake()
    original = fake.read

    def failed(path: str, subject: str) -> httpx.Response:
        response = original(path, subject)
        if response.status_code == 200 and "/audits/" in path:
            data = response.json()
            data.update(
                status="PROCESSING_FAILED",
                completedAt="2026-10-14T16:00:03Z",
                findings=[],
                error=fake.project_error(500, "AUDIT_TIMEOUT").json(),
            )
            return httpx.Response(200, json=data)
        return response

    fake.read = failed  # type: ignore[method-assign]
    client, _ = make_client(fake)
    report = SuiteReport.model_validate_json(run_once(client).content)
    assert outcomes(report)["P3-AUDIT-CLEAN"] == ("error", "system")
    assert "AUDIT_TIMEOUT" in next(
        result.message for result in report.results if result.test_id == "P3-AUDIT-CLEAN"
    )
    assert outcomes(report)["P3-AUDIT-FAILURE-ENVELOPE"] == ("pass", None)
    assert report.coverage.system.covered == 4  # AC-03/04/05/18 did not execute


def test_tester_defect_aborts_and_retains_outcomes(
    make_client: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(ctx: checks.SuiteContext) -> str:
        raise RuntimeError("private diagnostic must not be returned")

    monkeypatch.setitem(CHECKS, "upload_without_token", broken)
    client, _ = make_client()
    sid = setup_session(client)
    run = client.post("/test/run", json={"session_id": sid}, headers=AUTH)
    assert run.status_code == 500 and "private diagnostic" not in run.text
    stored = client.get("/test/report", params={"session_id": sid}, headers=AUTH)
    report = SuiteReport.model_validate_json(stored.content)
    assert report.results[0].outcome == Outcome.PASS
    assert (report.results[1].outcome, report.results[1].source) == (Outcome.ERROR, "tester")
    assert all(r.outcome == Outcome.SKIP for r in report.results[2:])
    assert client.post("/test/run", json={"session_id": sid}, headers=AUTH).status_code == 409


def test_setup_saves_plan_and_run_ignores_later_manifest_changes(
    make_client: Callable[..., Any], tmp_path: Path
) -> None:
    import shutil

    shutil.copytree(MANIFEST.parent, tmp_path / "fixtures")
    manifest = tmp_path / "fixtures" / "manifest.json"
    client, _ = make_client(fixture_manifest_path=manifest)
    sid = setup_session(client)
    session = client.app.state.tester.store.get(sid)
    assert session.plan is not None and session.setup == {}
    manifest.write_text("{}", encoding="utf-8")
    (manifest.parent / "expectations.json").unlink()
    run = client.post("/test/run", json={"session_id": sid}, headers=AUTH)
    assert run.status_code == 200
    report = SuiteReport.model_validate_json(run.content)
    assert len(report.results) == len(json.loads(session.plan)["manifest"]["tests"])


def test_recovery_uses_saved_plan_without_any_fixture_files(tmp_path: Path) -> None:
    suite = load_suite(MANIFEST, set(CHECKS))
    saved = json.loads(snapshot_suite(suite, MANIFEST))
    saved["fixture_root"] = str(tmp_path / "deleted")
    store = Store(tmp_path / "tester.sqlite3")
    store.create("crashed", "grader", BASE_URL, datetime.now(UTC), plan=json.dumps(saved))
    store.finish_setup("crashed", True)
    store.begin_run("crashed", "grader")
    done = [TestResult(test_id="P3-HEALTH", outcome="pass", message="Health matched")]
    store.save_progress(
        "crashed", done, {"P3-AUTH-UPLOAD": {"components": ["C1"], "acceptance": ["AC-15"]}}
    )
    services = ServicesForTester(
        settings=settings(tmp_path, fixture_manifest_path=tmp_path / "missing.json")
    )
    with TestClient(create_app(services=services)) as client:
        result = client.get("/test/report", params={"session_id": "crashed"}, headers=AUTH)
    report = SuiteReport.model_validate_json(result.content)
    assert report.results[0] == done[0]
    assert [r.test_id for r in report.results] == [t.test_id for t in suite.manifest.tests]
    assert report.coverage.system.covered == 1 and report.coverage.component.covered == 1


def test_legacy_running_session_is_closed_without_fabricating_plan(tmp_path: Path) -> None:
    store = Store(tmp_path / "tester.sqlite3")
    store.create("legacy", "grader", BASE_URL, datetime.now(UTC))
    store.finish_setup("legacy", True)
    store.begin_run("legacy", "grader")
    recorded = TestResult(test_id="old-id", outcome="pass", message="Recorded before upgrade")
    store.save_progress("legacy", [recorded])
    with TestClient(create_app(settings(tmp_path))) as client:
        report = SuiteReport.model_validate_json(
            client.get("/test/report", params={"session_id": "legacy"}, headers=AUTH).content
        )
    assert report.results == [recorded] and report.coverage.system.covered is None
    assert store.get("legacy").state == SessionState.ABORTED


def test_setup_validates_suite_before_contacting_target(
    make_client: Callable[..., Any], tmp_path: Path
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    client, fake = make_client(fixture_manifest_path=bad)
    response = client.post("/test/setup", json={"target_url": BASE_URL}, headers=AUTH)
    assert response.status_code == 500 and response.json()["source"] == "tester"
    assert fake.requests == []


@pytest.mark.parametrize("preparation_delay", [0.0, 0.3])
def test_late_dns_cannot_send_or_repeat_setup_after_timeout(
    make_client: Callable[..., Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preparation_delay: float,
) -> None:
    entered = threading.Event()
    gate = threading.Event()
    exited = threading.Event()
    fake = small_fake()

    def slow_resolver(*args: object, **kwargs: object) -> list[Any]:
        entered.set()
        gate.wait(5)
        exited.set()
        return address_resolver()(*args, **kwargs)

    # A fake setup clock isolates the DNS request's real wall-clock budget from fixture
    # and SQLite preparation. On Windows that preparation can exceed a 50 ms setup budget
    # and safely reject the call before DNS ever starts. The streaming tests separately
    # verify end-to-end setup timing with a real clock.
    services = ServicesForTester(
        settings=settings(tmp_path, tester_setup_timeout_seconds=0.2),
        transport=fake.transport(),
        resolver=slow_resolver,
        clock=FakeClock(),
    )
    app = create_app(services=services)
    load = app.state.tester.load_suite

    def prepare() -> Any:
        import time

        time.sleep(preparation_delay)  # Reproduce slow pre-call preparation on a CI runner.
        return load()

    monkeypatch.setattr(app.state.tester, "load_suite", prepare)
    try:
        with TestClient(app) as client:
            response = client.post("/test/setup", json={"target_url": BASE_URL}, headers=AUTH)
            assert response.status_code == 500 and response.json()["source"] == "communication"
            assert entered.is_set() and not exited.is_set()
            gate.set()
            assert exited.wait(2)
    finally:
        gate.set()
    assert fake.requests == []
    failed = Store(tmp_path / "tester.sqlite3").cleanup_candidates(BASE_URL, failed_only=True)
    assert len(failed) == 1


def test_encrypted_zip_is_real_and_password_required() -> None:
    import io
    import zipfile

    from tester.suite.workbooks import ZIP_FIXTURE_PASSWORD, clean_workbook, hostile_workbooks

    with (
        zipfile.ZipFile(io.BytesIO(hostile_workbooks()["encrypted_zip.xlsx"])) as encrypted,
        zipfile.ZipFile(io.BytesIO(clean_workbook())) as original,
    ):
        for info in encrypted.infolist():
            assert info.flag_bits & 1
            with pytest.raises(RuntimeError):
                encrypted.read(info)
            assert encrypted.read(info, pwd=ZIP_FIXTURE_PASSWORD) == original.read(info.filename)


def test_integrity_error_is_not_a_successful_hash_check(make_client: Callable[..., Any]) -> None:
    fake = small_fake()
    original = fake.read

    def corrupt(path: str, subject: str) -> httpx.Response:
        response = original(path, subject)
        if response.status_code == 200 and "/audits/" not in path:
            return fake.project_error(500, "SOURCE_INTEGRITY_FAILED")
        return response

    fake.read = corrupt  # type: ignore[method-assign]
    client, _ = make_client(fake)
    report = SuiteReport.model_validate_json(run_once(client).content)
    assert outcomes(report)["P3-VERSION-VALID"] == ("error", "system")
    assert outcomes(report)["P3-SOURCE-HASH"] == ("error", "system")
    assert report.coverage.system.covered == 6


def test_invalid_coverage_schema_is_fail_with_paired_nulls(make_client: Callable[..., Any]) -> None:
    client, _ = make_client(
        small_fake(code_coverage={"covered": 60, "total": 50, "basis": "Invalid"})
    )
    report = SuiteReport.model_validate_json(run_once(client).content)
    assert outcomes(report)["P3-CODE-REPORT"] == ("fail", None)
    assert report.coverage.code.covered is None and report.coverage.code.total is None


def test_wrong_system_report_session_is_fail_with_paired_nulls(
    make_client: Callable[..., Any],
) -> None:
    fake = small_fake()
    original = fake.instructor

    def wrong(request: httpx.Request, path: str) -> httpx.Response:
        response = original(request, path)
        if path == "/test/report" and response.status_code == 200:
            return httpx.Response(200, json=response.json() | {"session_id": "other-session"})
        return response

    fake.instructor = wrong  # type: ignore[method-assign]
    client, _ = make_client(fake)
    report = SuiteReport.model_validate_json(run_once(client).content)
    assert outcomes(report)["P3-CODE-REPORT"] == ("fail", None)
    assert report.coverage.code.covered is None


def test_first_unknown_id_failure_does_not_omit_other_id_cases(
    make_client: Callable[..., Any],
) -> None:
    fake = small_fake()
    original = fake.handle

    def incorrect(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/workbooks/unknown-workbook/versions/unknown-version":
            return httpx.Response(200, json={})
        return original(request)

    fake.handle = incorrect  # type: ignore[method-assign]
    client, _ = make_client(fake)
    report = SuiteReport.model_validate_json(run_once(client).content)
    assert outcomes(report)["P3-NOT-FOUND"] == ("fail", None)
    for test_id in ("P3-VERSION-MISMATCH", "P3-AUDIT-MISMATCH", "P3-AUDIT-UNKNOWN"):
        assert outcomes(report)[test_id] == ("pass", None)


def test_target_specific_tokens_and_base_prefix(
    make_client: Callable[..., Any], tmp_path: Path
) -> None:

    prefix = BASE_URL + "/system"
    tokens = {identity: "second-target-" + identity for identity in TOKENS}
    fake = small_fake()
    fake.subjects = {token: identity for identity, token in tokens.items()}
    seen: list[httpx.Request] = []

    def prefixed(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path.startswith("/system/")
        forwarded = httpx.Request(
            request.method,
            str(request.url).replace("/system/", "/", 1),
            headers=request.headers,
            content=request.content,
        )
        return fake.handle(forwarded)

    config = settings(
        tmp_path,
        allowed_target_urls=[BASE_URL, prefix],
        TARGET_AUTH_MAP_JSON=access_map() | {prefix: tokens},
    )
    services = ServicesForTester(
        settings=config,
        transport=httpx.MockTransport(prefixed),
        resolver=address_resolver(),
        sleep=lambda seconds: None,
    )
    with TestClient(create_app(services=services)) as client:
        setup = client.post("/test/setup", json={"target_url": prefix}, headers=AUTH)
        assert setup.status_code == 201
        sid = setup.json()["session_id"]
        run = client.post("/test/run", json={"session_id": sid}, headers=AUTH)
        assert run.status_code == 200
        report = SuiteReport.model_validate_json(run.content)
    assert not any(r.outcome in (Outcome.FAIL, Outcome.ERROR) for r in report.results)
    assert seen[0].headers["authorization"] == "Bearer " + tokens["test-admin"]
    assert all(not any(token in report.model_dump_json() for token in tokens.values()) for _ in [0])


def test_compressed_target_responses_are_decoded_once(make_client: Callable[..., Any]) -> None:
    import gzip

    fake = small_fake()
    original = fake.handle

    def compressed(request: httpx.Request) -> httpx.Response:
        response = original(request)
        return httpx.Response(
            response.status_code,
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
            content=gzip.compress(response.content),
        )

    fake.handle = compressed  # type: ignore[method-assign]
    client, _ = make_client(fake)
    report = SuiteReport.model_validate_json(run_once(client).content)
    assert not any(r.outcome in (Outcome.FAIL, Outcome.ERROR) for r in report.results)


@pytest.mark.parametrize("failed", [False, True])
def test_operator_cli_recovers_lost_run_and_blocks_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failed: bool,
) -> None:
    from app.contracts.models import CoverageMeasure, SuiteCoverage
    from scripts import run_suite as cli
    from tester.target import TargetClient

    manifest = SuiteManifest.model_validate_json(MANIFEST.read_bytes())
    code = CoverageMeasure(
        covered=None, total=None, basis="Unit-test response; no target measurement"
    )
    report = SuiteReport(
        session_id="cli-session",
        target_url=BASE_URL,
        results=[
            TestResult(
                test_id=t.test_id,
                outcome="fail" if failed and t.test_id == "P3-HEALTH" else "pass",
                message="Controlled CLI test response",
            )
            for t in manifest.tests
        ],
        coverage=SuiteCoverage(code=code, component=code, system=code),
    )
    requests: list[httpx.Request] = []
    polls = [0]

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/test/setup":
            return httpx.Response(201, json={"session_id": "cli-session"})
        if request.url.path == "/test/run":
            raise httpx.ReadTimeout("response lost", request=request)
        polls[0] += 1
        if polls[0] == 1:
            return httpx.Response(409, json={"message": "Still running", "source": "tester"})
        return httpx.Response(200, content=report.model_dump_json())

    monkeypatch.setattr(
        cli,
        "TargetClient",
        lambda base, access, environment, deadline: TargetClient(
            base,
            access,
            environment,
            deadline,
            transport=httpx.MockTransport(handle),
            resolver=address_resolver(),
        ),
    )
    monkeypatch.setattr(cli, "REPORT_POLL_SECONDS", 0)
    monkeypatch.setenv("TEAM9_TESTER_TOKEN", OPERATOR)
    output, record = tmp_path / "report.json", tmp_path / "record.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_suite",
            "--tester-url",
            "http://127.0.0.1:8001",
            "--target-url",
            BASE_URL,
            "--manifest",
            str(MANIFEST),
            "--output",
            str(output),
            "--record",
            str(record),
        ],
    )
    assert cli.main() == (1 if failed else 0)
    assert len([r for r in requests if r.url.path == "/test/run"]) == 1
    assert len([r for r in requests if r.url.path == "/test/setup"]) == 1
    assert len([r for r in requests if r.url.path == "/test/report"]) == 2
    assert output.read_bytes() == report.model_dump_json().encode()
    assert json.loads(record.read_bytes())["counts"]["fail"] == int(failed)
    assert OPERATOR not in capsys.readouterr().out and OPERATOR not in record.read_text()


def test_duplicate_upload_resource_ids_are_rejected(make_client: Callable[..., Any]) -> None:
    fake = small_fake()
    original = fake.upload

    def duplicate(request: httpx.Request, subject: str) -> httpx.Response:
        response = original(request, subject)
        if response.status_code == 202:
            data = response.json()
            data["versionId"] = data["workbookId"]
            return httpx.Response(202, json=data)
        return response

    fake.upload = duplicate  # type: ignore[method-assign]
    client, _ = make_client(fake)
    report = SuiteReport.model_validate_json(run_once(client).content)
    assert outcomes(report)["P3-UPLOAD-CLEAN"] == ("fail", None)
    assert outcomes(report)["P3-AUDIT-CLEAN"] == ("skip", None)


@pytest.mark.parametrize("field", ["createdAt", "startedAt"])
def test_wire_timestamps_require_utc_z(make_client: Callable[..., Any], field: str) -> None:
    fake = small_fake()
    original = fake.read

    def offset(path: str, subject: str) -> httpx.Response:
        response = original(path, subject)
        if response.status_code == 200 and field in response.json():
            data = response.json()
            data[field] = data[field].replace("Z", "+00:00")
            return httpx.Response(200, json=data)
        return response

    fake.read = offset  # type: ignore[method-assign]
    client, _ = make_client(fake)
    report = SuiteReport.model_validate_json(run_once(client).content)
    test_id = "P3-VERSION-VALID" if field == "createdAt" else "P3-AUDIT-CLEAN"
    assert outcomes(report)[test_id] == ("fail", None)


def test_non_object_version_body_is_schema_failure() -> None:
    from app.contracts.models import VersionResponse

    with pytest.raises(checks.CheckFailed, match="VersionResponse"):
        checks.parse(httpx.Response(200, json=[]), VersionResponse, "Version")


def test_interrupted_setup_queues_compensation_without_repeating_post(tmp_path: Path) -> None:
    store = Store(tmp_path / "tester.sqlite3")
    store.create("lost-setup", "grader", BASE_URL, datetime.now(UTC))
    fake = small_fake()
    services = ServicesForTester(
        settings=settings(tmp_path), transport=fake.transport(), resolver=address_resolver()
    )
    with TestClient(create_app(services=services)) as client:
        assert fake.requests == []
        assert store.get("lost-setup").state == SessionState.SETUP_FAILED
        assert store.cleanup_candidates(BASE_URL, failed_only=True) == ["lost-setup"]
        assert (
            client.post("/test/run", json={"session_id": "lost-setup"}, headers=AUTH).status_code
            == 409
        )
        setup_session(client)
    assert len(fake.requests) == 1
    body = json.loads(fake.requests[0].content)
    assert body["session_id"] != "lost-setup"
    assert body["setup"] == {"cleanup_session_ids": ["lost-setup"]}


@pytest.mark.parametrize("wrong", ["empty", "classification"])
def test_inaccurate_completed_audit_still_supplies_repeatability_baseline(
    make_client: Callable[..., Any], wrong: str
) -> None:
    def mutate(findings: list[dict[str, str]]) -> list[dict[str, str]]:
        if wrong == "empty":
            return []
        return [finding | {"classification": "MANUAL_REVIEW"} for finding in findings]

    client, fake = make_client(small_fake(mutate_findings=mutate))
    report = SuiteReport.model_validate_json(run_once(client).content)
    assert outcomes(report)["P3-AUDIT-SEEDED"] == ("fail", None)
    repeated = [result for result in report.results if result.test_id.startswith("P3-DETERMINISM")]
    assert len(repeated) == 4 and all(result.outcome == Outcome.PASS for result in repeated)
    assert (
        len([audit for audit in fake.audits.values() if audit.file_name == "seeded_issues.xlsx"])
        == 5
    )


def test_inaccurate_and_unstable_findings_report_both_failures(
    make_client: Callable[..., Any],
) -> None:
    seeded = [0]

    def mutate(findings: list[dict[str, str]]) -> list[dict[str, str]]:
        if len(findings) == 6:
            seeded[0] += 1
            classification = "REQUIRES_USER_INPUT" if seeded[0] == 2 else "MANUAL_REVIEW"
            return [finding | {"classification": classification} for finding in findings]
        return findings

    client, _ = make_client(small_fake(mutate_findings=mutate))
    report = SuiteReport.model_validate_json(run_once(client).content)
    assert outcomes(report)["P3-AUDIT-SEEDED"] == ("fail", None)
    assert outcomes(report)["P3-DETERMINISM"] == ("fail", None)
    for test_id in ("P3-DETERMINISM-3", "P3-DETERMINISM-4", "P3-DETERMINISM-5"):
        assert outcomes(report)[test_id] == ("pass", None)
    assert seeded[0] == 5


def test_explicitly_granted_reviewer_reads_version_and_audit(
    make_client: Callable[..., Any],
) -> None:
    client, fake = make_client(small_fake(reviewer_grants={"reviewer": {"userA"}}))
    report = SuiteReport.model_validate_json(run_once(client).content)
    for test_id in ("P3-VERSION-REVIEWER", "P3-AUDIT-REVIEWER"):
        assert outcomes(report)[test_id] == ("pass", None)
    for test_id in ("P3-VERSION-FOREIGN", "P3-AUDIT-FOREIGN"):
        assert outcomes(report)[test_id] == ("pass", None)
    reviewer_requests = [r for r in fake.requests if fake.subject(r) == "reviewer"]
    assert len(reviewer_requests) == 2 and all(r.method == "GET" for r in reviewer_requests)
    assert any("/audits/" in r.url.path for r in reviewer_requests)
    assert TOKENS["reviewer"] not in report.model_dump_json()


@pytest.mark.parametrize("grants", [{}, {"reviewer": {"userB"}}])
def test_reviewer_role_without_matching_grant_is_reported_as_failure(
    make_client: Callable[..., Any], grants: dict[str, set[str]]
) -> None:
    client, _ = make_client(small_fake(reviewer_grants=grants))
    report = SuiteReport.model_validate_json(run_once(client).content)
    assert outcomes(report)["P3-VERSION-REVIEWER"] == ("fail", None)
    assert outcomes(report)["P3-AUDIT-REVIEWER"] == ("fail", None)
    assert outcomes(report)["P3-VERSION-FOREIGN"] == ("pass", None)
    assert outcomes(report)["P3-AUDIT-FOREIGN"] == ("pass", None)


@pytest.mark.parametrize("resource", ["version", "audit"])
def test_reviewer_success_must_return_requested_resource(
    make_client: Callable[..., Any], resource: str
) -> None:
    fake = small_fake()
    original = fake.read

    def wrong(path: str, subject: str) -> httpx.Response:
        response = original(path, subject)
        if subject == "reviewer" and response.status_code == 200:
            is_audit = "/audits/" in path
            if is_audit == (resource == "audit"):
                return httpx.Response(
                    200, json=response.json() | {"workbookId": "foreign-resource"}
                )
        return response

    fake.read = wrong  # type: ignore[method-assign]
    client, _ = make_client(fake)
    report = SuiteReport.model_validate_json(run_once(client).content)
    assert outcomes(report)["P3-" + resource.upper() + "-REVIEWER"] == ("fail", None)
