"""Deployed black-box checks of a running tester. Skipped unless explicitly configured.

Set TEAM9_TESTER_URL, TEAM9_TARGET_URL and TEAM9_TESTER_TOKEN (operator token). The target must
be in the tester's ALLOWED_TARGET_URLS and TARGET_AUTH_MAP_JSON. No test code changes are needed
to point this at a local, AWS or Team 12 deployment.
"""

import os
from collections.abc import Iterator

import httpx
import pytest

from app.contracts.models import Outcome, SuiteReport
from app.contracts.models import TestEndpointError as EndpointError

TESTER = os.environ.get("TEAM9_TESTER_URL")
TARGET = os.environ.get("TEAM9_TARGET_URL")
TOKEN = os.environ.get("TEAM9_TESTER_TOKEN")

pytestmark = pytest.mark.skipif(
    not (TESTER and TARGET and TOKEN), reason="live tester URL/target/token not configured"
)


@pytest.fixture(scope="module")
def client() -> Iterator[httpx.Client]:
    assert TESTER and TOKEN
    with httpx.Client(
        base_url=TESTER.rstrip("/"),
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=httpx.Timeout(60, read=int(os.environ.get("TEAM9_RUN_READ_TIMEOUT", "630"))),
        trust_env=False,
    ) as live:
        yield live


def test_unauthenticated_calls_are_refused() -> None:
    assert TESTER
    with httpx.Client(base_url=TESTER.rstrip("/"), timeout=10, trust_env=False) as anonymous:
        response = anonymous.post("/test/setup", json={"target_url": TARGET})
    assert response.status_code == 401
    EndpointError.model_validate(response.json())


def test_unapproved_target_is_refused(client: httpx.Client) -> None:
    response = client.post("/test/setup", json={"target_url": "https://unapproved.invalid"})
    assert response.status_code == 400
    assert response.json()["source"] == "tester"


def test_single_use_session_and_stored_report(client: httpx.Client) -> None:
    setup = client.post("/test/setup", json={"target_url": TARGET})
    assert setup.status_code == 201, setup.text
    session_id = setup.json()["session_id"]
    pending = client.get("/test/report", params={"session_id": session_id})
    assert pending.status_code == 409
    run = client.post("/test/run", json={"session_id": session_id})
    assert run.status_code in (200, 500), run.text
    stored = client.get("/test/report", params={"session_id": session_id})
    assert stored.status_code == 200
    report = SuiteReport.model_validate_json(stored.content)
    assert report.session_id == session_id and report.target_url == TARGET
    if run.status_code == 200:
        assert stored.content == run.content
    else:
        assert run.json()["source"] == "tester"
        assert any(r.outcome == Outcome.ERROR and r.source == "tester" for r in report.results)
    again = client.post("/test/run", json={"session_id": session_id})
    assert again.status_code == 409


def test_lost_run_response_is_recovered_without_another_run(client: httpx.Client) -> None:
    import time

    setup = client.post("/test/setup", json={"target_url": TARGET})
    assert setup.status_code == 201
    sid = setup.json()["session_id"]
    try:
        # On the local fake this disconnects while the suite is still running.
        # A faster approved remote tester may complete before this read timeout.
        run = client.post(
            "/test/run", json={"session_id": sid}, timeout=httpx.Timeout(60, read=0.05)
        )
        assert run.status_code in (200, 500)
    except httpx.TransportError:
        pass
    deadline = time.monotonic() + int(os.environ.get("TEAM9_RUN_READ_TIMEOUT", "630"))
    while True:
        stored = client.get("/test/report", params={"session_id": sid})
        if stored.status_code != 409 or time.monotonic() >= deadline:
            break
        time.sleep(1)
    assert stored.status_code == 200
    report = SuiteReport.model_validate_json(stored.content)
    assert report.session_id == sid
    assert client.post("/test/run", json={"session_id": sid}).status_code == 409
