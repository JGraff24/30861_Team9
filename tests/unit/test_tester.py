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
from tester.suite.manifest import SuiteConfigurationError, load_suite
from tester.target import CommunicationError, check_address
from tests.support.fake_system import BASE_URL, TOKENS, FakeSystem, access_map, address_resolver

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "tests" / "fixtures" / "manifest.json"
OPERATOR = "grader-operator-token"
OTHER_OPERATOR = "other-operator-token"
AUTH = {"Authorization": f"Bearer {OPERATOR}"}
OTHER = {"Authorization": f"Bearer {OTHER_OPERATOR}"}
SMALL_LIMIT = 60_000


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
        fake = fake or FakeSystem(upload_limit=SMALL_LIMIT)
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
    assert all(r.outcome == Outcome.PASS for r in report.results), outcomes(report)
    assert (report.coverage.component.covered, report.coverage.component.total) == (6, 6)
    assert (report.coverage.system.covered, report.coverage.system.total) == (8, 8)
    assert (report.coverage.code.covered, report.coverage.code.total) == (40, 50)
    # Every project call carries a session header; only P3-20 names an unknown session.
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


def test_cleanup_of_prior_sessions_is_opt_in(make_client: Callable[..., Any]) -> None:
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
    enabled, _ = make_client(recording, tester_cleanup_prior_sessions=True)
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
    client, fake = make_client(FakeSystem(upload_limit=SMALL_LIMIT, gate=gate))
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
    assert results["P3-01"] == ("error", "system")
    assert results["P3-15"] == ("fail", None)
    assert results["P3-19"] == ("skip", None)  # depends on a passing seeded audit
    # Errors and skips earn no coverage; P3-01 declares none anyway.
    assert report.coverage.system.covered == 7


def test_communication_failure_is_reported_per_test(make_client: Callable[..., Any]) -> None:
    client, fake = make_client()
    session_id = setup_session(client)
    fake.drop_paths.add("/api/v1/health")
    report = SuiteReport.model_validate_json(
        client.post("/test/run", json={"session_id": session_id}, headers=AUTH).content
    )
    assert outcomes(report)["P3-01"] == ("error", "communication")
    health_calls = [r for r in fake.requests if r.url.path == "/api/v1/health"]
    assert len(health_calls) == 4  # one try plus three GET transport retries


def test_post_is_never_retried(make_client: Callable[..., Any]) -> None:
    client, fake = make_client()
    session_id = setup_session(client)
    fake.drop_paths.add("/api/v1/workbooks")
    client.post("/test/run", json={"session_id": session_id}, headers=AUTH)
    uploads = [r for r in fake.requests if r.url.path == "/api/v1/workbooks"]
    planned_uploads = 11  # P3-02 to P3-12, each attempted once; P3-19 then skips
    assert len(uploads) == planned_uploads


def test_audit_that_never_finishes_fails_within_deadline(make_client: Callable[..., Any]) -> None:
    client, _ = make_client(FakeSystem(upload_limit=SMALL_LIMIT, processing_polls=10**6))
    session_id = setup_session(client)
    report = SuiteReport.model_validate_json(
        client.post("/test/run", json={"session_id": session_id}, headers=AUTH).content
    )
    results = outcomes(report)
    assert results["P3-14"] == ("fail", None)
    assert "PROCESSING after 75 s" in next(
        r.message for r in report.results if r.test_id == "P3-14"
    )


def test_redirects_are_not_followed(make_client: Callable[..., Any]) -> None:
    fake = FakeSystem(upload_limit=SMALL_LIMIT)
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
    assert outcomes(report)["P3-01"] == ("fail", None)
    assert all(r.url.host != "169.254.169.254" for r in fake.requests)


def test_deadline_abort_stores_report_and_returns_500(make_client: Callable[..., Any]) -> None:
    clock = FakeClock()
    fake = FakeSystem(upload_limit=SMALL_LIMIT)
    original = fake.handle

    def slow_health(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/health":
            clock.now += 601  # the suite budget is spent while P3-01 is answered
        return original(request)

    fake.handle = slow_health  # type: ignore[method-assign]
    client, _ = make_client(fake, clock)
    session_id = setup_session(client)
    run = client.post("/test/run", json={"session_id": session_id}, headers=AUTH)
    assert run.status_code == 500 and run.json()["source"] == "tester"
    stored = client.get("/test/report", params={"session_id": session_id}, headers=AUTH)
    report = SuiteReport.model_validate_json(stored.content)
    assert report.results[0].outcome == Outcome.PASS
    assert (report.results[1].outcome, report.results[1].source) == (Outcome.ERROR, "tester")
    assert {r.outcome for r in report.results[2:]} == {Outcome.SKIP}
    assert report.coverage.code.covered is None and report.coverage.code.total is None
    again = client.post("/test/run", json={"session_id": session_id}, headers=AUTH)
    assert again.status_code == 409


def test_invalid_suite_configuration_does_not_consume_session(
    make_client: Callable[..., Any], tmp_path: Path
) -> None:
    broken = tmp_path / "manifest.json"
    broken.write_text("{}", encoding="utf-8")
    client, fake = make_client(fixture_manifest_path=broken)
    session_id = setup_session(client)
    run = client.post("/test/run", json={"session_id": session_id}, headers=AUTH)
    assert run.status_code == 500 and run.json()["source"] == "tester"
    session = Store(tmp_path / "tester.sqlite3").get(session_id)
    assert session is not None and session.state == SessionState.READY


def test_interrupted_run_is_recovered_as_aborted(tmp_path: Path) -> None:
    store = Store(tmp_path / "tester.sqlite3")
    store.create("crashed-session", "grader", BASE_URL, datetime.now(UTC))
    store.finish_setup("crashed-session", ready=True)
    store.begin_run("crashed-session", "grader")
    done = TestResult(test_id="P3-01", outcome=Outcome.PASS, message="Health matched.")
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
