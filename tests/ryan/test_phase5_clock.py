"""Clock HTTP and setup boundaries against the real Phase 5 persistence services."""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.contracts.errors import StorageError
from app.contracts.models import SessionState
from app.main import create_app
from app.storage.sqlite import SQLiteRepository
from tests.unit import test_phase5_backend as backend
from tests.unit.test_phase5_backend import (
    ADMIN,
    INITIAL,
    headers,
    policy,
    prepare,
    repository,
    upload,
    worker,
)


@pytest.fixture
def client(tmp_path):
    yield from backend.client.__wrapped__(tmp_path)


PATH = "/api/v1/test/clock"
NOW = "2027-01-01T00:00:00Z"


def clock(http, time=NOW, sid="s", token="admin"):
    return http.post(PATH, json={"currentTime": time}, headers=headers(token, sid))


def error(response, status, code):
    assert response.status_code == status, response.text
    assert set(response.json()) == {"code", "message", "requestId", "details"}
    assert response.json()["code"] == code
    assert response.json()["requestId"] == response.headers["X-Request-ID"]


@pytest.mark.parametrize(
    "token,sid,status,code",
    [
        ("invalid", None, 404, "TEST_CLOCK_DISABLED"),
        ("invalid", "s", 401, "UNAUTHORIZED"),
        ("owner", "s", 403, "FORBIDDEN"),
        ("reviewer", "s", 403, "FORBIDDEN"),
        ("other-admin", "s", 403, "FORBIDDEN"),
        ("admin", "unknown", 400, "INVALID_TEST_SESSION"),
        ("admin", "bad id", 400, "INVALID_REQUEST"),
    ],
)
def test_clock_authorization_before_malformed_body(client, token, sid, status, code):
    response = client.post(PATH, content=b"{", headers=headers(token, sid))
    error(response, status, code)
    assert repository(client).effective_now("s") == INITIAL


def test_gate_and_session_states(client):
    settings = client.app.state.services.settings
    settings.enable_test_clock = False
    error(clock(client, token="invalid"), 404, "TEST_CLOCK_DISABLED")
    prepare(client, "legacy")
    settings.enable_test_clock = True
    error(clock(client, sid="legacy"), 409, "LEGACY_SESSION_CLOCK")
    prepare(client, "failed")
    repository(client).mark_session_failed("failed", "preparation failed")
    error(clock(client, sid="failed"), 409, "SESSION_NOT_READY")
    client.app.state.services.session_service.cleanup("failed", ADMIN)
    error(clock(client, sid="failed"), 400, "INVALID_TEST_SESSION")
    error(
        client.post(PATH, json={"currentTime": NOW}, headers={"X-Test-Session": "s"}),
        401,
        "UNAUTHORIZED",
    )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"currentTime":"2027-01-01T00:00:00Z","currentTime":"2027-02-01T00:00:00Z"}',
        b'{"currentTime":"2027-01-01T00:00:00Z","extra":1}',
        b'{"currentTime":NaN}',
        b'{"currentTime":1e999}',
        b"[]",
        b"{}",
        b"\xff",
        b'{"currentTime":"2027-01-01T00:00:00+00:00"}',
        b'{"currentTime":"2027-02-29T00:00:00Z"}',
        b'{"currentTime":"2027-01-01T00:00:00.1234567Z"}',
    ],
)
def test_strict_clock_json(client, raw):
    error(
        client.post(
            PATH, content=raw, headers=headers("admin") | {"Content-Type": "application/json"}
        ),
        400,
        "INVALID_REQUEST",
    )
    assert repository(client).effective_now("s") == INITIAL


def test_setup_rejects_before_cleanup_and_preserves_testing_envelope(client):
    raw = (
        b'{"session_id":"new","setup":{"cleanup_session_ids":["s"],"clock":{'
        b'"currentTime":"2027-01-01T00:00:00Z","currentTime":"2028-01-01T00:00:00Z"}}}'
    )
    for data, status in ((raw, 400), (b" " * 1_048_577, 413)):
        result = client.post(
            "/test/setup",
            content=data,
            headers=headers("admin") | {"Content-Type": "application/json"},
        )
        assert result.status_code == status
        assert set(result.json()) == {"message", "source"}
        assert result.json()["source"] == "system"
        assert repository(client).get_test_session("new") is None
        assert repository(client).get_test_session("s").state == SessionState.READY
    client.app.state.services.settings.enable_test_clock = False
    result = client.post(
        "/test/setup",
        json={
            "session_id": "new",
            "setup": {"clock": {"currentTime": NOW}, "cleanup_session_ids": ["s"]},
        },
        headers=headers("admin"),
    )
    assert result.status_code == 403
    assert repository(client).get_test_session("new") is None
    assert repository(client).get_test_session("s").state == SessionState.READY


def test_clock_replay_busy_isolation_and_restart(client):
    prepare(client, "other", {"clock": {"currentTime": "2028-02-29T12:00:00.123456Z"}})
    assert clock(client).json() == {
        "currentTime": NOW,
        "triggeredAudits": [],
        "triggeredNotifications": [],
    }
    items = {}
    for sid in ("s", "other", None):
        items[sid] = upload(client, session=sid, clean=False)
        policy(client, items[sid]["workbookId"], {"enabled": True, "intervalDays": 1}, sid)
        if sid != "s":
            worker(client)  # also drains earlier accepted s work
    worker(client)
    before_other = repository(client).get_schedule(items["other"]["workbookId"])
    before_ordinary = repository(client).get_schedule(items[None]["workbookId"])
    result = clock(client, "2027-04-01T00:00:00Z")
    assert result.status_code == 200, result.text
    assert len(result.json()["triggeredAudits"]) == 1
    error(clock(client, "2027-04-02T00:00:00Z"), 409, "ACTIVE_OPERATION")
    assert clock(client, "2027-04-01T00:00:00Z").json()["triggeredAudits"] == []
    worker(client)
    assert repository(client).get_schedule(items["other"]["workbookId"]) == before_other
    assert repository(client).get_schedule(items[None]["workbookId"]) == before_ordinary
    assert clock(client, sid="other", time="2028-02-29T12:00:00.123456Z").status_code == 200
    with TestClient(create_app(client.app.state.services.settings)) as restarted:
        assert clock(restarted, "2027-04-01T00:00:00Z").json()["triggeredAudits"] == []
        error(clock(restarted), 400, "CLOCK_REWIND")


def test_late_storage_failure_rolls_back_and_lost_ack_replays(client):
    item = upload(client, clean=False)
    worker(client)
    policy(
        client,
        item["workbookId"],
        {"enabled": True, "intervalDays": 1, "complianceDeadline": "2027-04-26"},
    )
    with patch.object(
        SQLiteRepository, "_save_session", side_effect=StorageError("private failure")
    ):
        error(clock(client, "2027-01-26T00:00:00Z"), 500, "STORAGE_FAILED")
    assert repository(client).effective_now("s") == INITIAL
    assert not repository(client).get_notifications(item["workbookId"]).notifications
    first = clock(client, "2027-01-26T00:00:00Z").json()
    assert len(first["triggeredAudits"]) == len(first["triggeredNotifications"]) == 1
    # Caller lost the first response; identical replay must not claim the same IDs.
    assert clock(client, "2027-01-26T00:00:00Z").json() == {
        "currentTime": "2027-01-26T00:00:00Z",
        "triggeredAudits": [],
        "triggeredNotifications": [],
    }
    worker(client)
    client.app.state.services.session_service.cleanup("s", ADMIN)
    error(clock(client), 400, "INVALID_TEST_SESSION")
    assert repository(client).get_test_session("s").clock_current_time is None


def test_real_worker_lease_not_expired_by_calendar_jump(client):
    item = upload(client)
    repo = repository(client)
    now = datetime.now(UTC)
    claimed = repo.claim_next_job("lease-test", now, 120)
    assert claimed is not None
    assert clock(client, "2099-01-01T00:00:00Z").status_code == 200
    repo.recover_stale_jobs(datetime.now(UTC))
    audit = repo.get_audit(item["workbookId"], item["versionId"], item["auditId"])
    assert audit.status.value == "PROCESSING"
    with repo._db() as db:
        import json

        job = json.loads(
            db.execute("SELECT payload FROM jobs WHERE audit_id=?", (item["auditId"],)).fetchone()[
                0
            ]
        )
    assert datetime.fromisoformat(job["lease_expires_at"]).year == now.year


def test_clock_deadline_does_not_wait_for_blocked_service(client, monkeypatch):
    import asyncio
    from threading import Event

    import httpx

    from app.api import routes
    from app.contracts.phase5 import ClockResponse
    from app.services.lifecycle import LifecycleService

    entered, release, finished = Event(), Event(), Event()

    def blocked(*args):
        entered.set()
        try:
            release.wait(5)
            return ClockResponse(currentTime=INITIAL, triggeredAudits=[], triggeredNotifications=[])
        finally:
            finished.set()

    monkeypatch.setattr(LifecycleService, "advance_clock", blocked)
    monkeypatch.setattr(routes, "CLOCK_TIMEOUT_SECONDS", 0.05)

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app), base_url="http://local"
        ) as http:
            response = await asyncio.wait_for(
                http.post(PATH, json={"currentTime": NOW}, headers=headers("admin")), timeout=1
            )
            assert entered.is_set() and not finished.is_set()
            error(response, 503, "SERVICE_UNAVAILABLE")

    try:
        asyncio.run(check())
    finally:
        release.set()
        assert finished.wait(1)


def test_clock_body_bound_and_openapi(client):
    error(
        client.post(
            PATH,
            content=b" " * 1_048_577,
            headers=headers("admin") | {"Content-Type": "application/json"},
        ),
        413,
        "REQUEST_TOO_LARGE",
    )
    operation = client.get("/openapi.json").json()["paths"][PATH]["post"]
    assert operation["security"] == [{"BearerAuth": []}]
    assert operation["parameters"][0]["required"] is True
    assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ClockRequest"
    )
    assert "200" in operation["responses"] and "409" in operation["responses"]
