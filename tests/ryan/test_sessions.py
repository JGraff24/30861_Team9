from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import SystemSettings
from app.contracts.errors import (
    DuplicateIdentifierError,
    ProjectException,
    SessionBusyError,
    StorageError,
)
from app.contracts.models import CoverageMeasure, Principal, Role, SessionState
from app.contracts.ports import SystemServices
from app.test_interface.routes import create_router
from app.test_interface.service import PersistentSessionService


class Repository:
    def __init__(self):
        self.sessions = {}
        self.lock = Lock()
        self.busy = False
        self.reads = 0

    def create_test_session(self, session):
        with self.lock:
            if session.session_id in self.sessions:
                raise DuplicateIdentifierError()
            self.sessions[session.session_id] = session

    def get_test_session(self, sid):
        return self.sessions.get(sid)

    def mark_session_ready(self, sid):
        self.sessions[sid] = self.sessions[sid].model_copy(update={"state": SessionState.READY})

    def mark_session_failed(self, sid, reason):
        state = self.sessions[sid].state
        self.sessions[sid] = self.sessions[sid].model_copy(
            update={
                "state": state if state == SessionState.CLEANED else SessionState.FAILED,
                "cleanup_error": reason,
            }
        )

    def begin_session_cleanup(self, sid, owner):
        assert self.sessions[sid].owner_subject == owner
        if self.busy:
            raise SessionBusyError()
        self.sessions[sid] = self.sessions[sid].model_copy(update={"state": SessionState.CLEANED})
        return [f"test/{sid}/blob"]

    def finish_session_cleanup(self, sid):
        self.sessions[sid] = self.sessions[sid].model_copy(update={"cleanup_error": None})

    def summarize_measurements(self, sid):
        self.reads += 1
        return CoverageMeasure(covered=None, total=None, basis="No executions.")


@pytest.fixture
def context():
    repo = Repository()
    blobs = Mock()
    settings = SystemSettings(enable_test_interface=True, test_session_users={"admin": ["user"]})
    service = PersistentSessionService(repo, blobs, settings)
    admin = Principal(subject="admin", role=Role.TEST_ADMIN)
    return repo, blobs, service, admin


def test_concurrency_and_isolation(context):
    repo, blobs, service, admin = context
    with ThreadPoolExecutor() as pool:
        futures = [pool.submit(service.prepare, "same", admin, {}) for _ in range(8)]
    assert sum(f.exception() is None for f in futures) == 1
    service.prepare("other", admin, {})
    service.require_ready("same", Principal(subject="user", role=Role.USER))
    with pytest.raises(ProjectException):
        service.require_ready("same", Principal(subject="outsider", role=Role.USER))
    before = dict(repo.sessions)
    service.get_code_coverage("same", admin)
    assert repo.sessions == before
    service.cleanup("same", admin)
    assert repo.sessions["other"].state == SessionState.READY
    blobs.delete_test_source.assert_called_once_with("test/same/blob", "same")
    with pytest.raises(DuplicateIdentifierError):
        service.prepare("same", admin, {})


def test_failure_and_cleanup_resume(context):
    repo, blobs, service, admin = context
    blobs.health.return_value = False
    with pytest.raises(StorageError):
        service.prepare("failed", admin, {})
    assert repo.sessions["failed"].state == SessionState.FAILED
    blobs.health.return_value = True
    service.prepare("old", admin, {})
    repo.busy = True
    with pytest.raises(SessionBusyError):
        service.cleanup("old", admin)
    assert repo.sessions["old"].state == SessionState.READY
    repo.busy = False
    blobs.delete_test_source.side_effect = RuntimeError("private")
    with pytest.raises(StorageError):
        service.cleanup("old", admin)
    assert repo.sessions["old"].state == SessionState.CLEANED
    assert repo.sessions["old"].cleanup_error
    blobs.delete_test_source.side_effect = None
    service.cleanup("old", admin)
    assert repo.sessions["old"].cleanup_error is None


def client(context, enabled=True):
    repo, blobs, service, admin = context
    auth = Mock()

    def require(token):
        if token != "admin":
            raise ProjectException(401, "UNAUTHORIZED", "Authentication required.")
        return admin

    auth.require_test_admin.side_effect = require
    app = FastAPI()
    app.include_router(
        create_router(
            SystemServices(
                settings=SystemSettings(enable_test_interface=enabled),
                repository=repo,
                blob_store=blobs,
                session_service=service,
                auth_service=auth,
            )
        )
    )
    return TestClient(app)


def test_http_contract(context):
    http = client(context)
    headers = {"Authorization": "Bearer admin"}
    assert http.post("/test/setup", json={}).status_code == 401
    for body in ({}, {"session_id": "bad id"}, {"session_id": "x", "setup": {"bad": 1}}):
        response = http.post("/test/setup", json=body, headers=headers)
        assert response.status_code == 400
        assert set(response.json()) == {"message", "source"}
    assert http.post("/test/setup", content="{", headers=headers).status_code == 400
    assert http.post(
        "/test/setup", json={"session_id": "x", "setup": {}}, headers=headers
    ).json() == {"session_id": "x"}
    assert http.post("/test/setup", json={"session_id": "x"}, headers=headers).status_code == 409
    assert http.get("/test/report?session_id=x", headers=headers).status_code == 200
    original = context[0].summarize_measurements
    context[0].summarize_measurements = Mock(side_effect=ValueError("private storage failure"))
    failed = http.get("/test/report?session_id=x", headers=headers)
    assert failed.status_code == 500
    assert failed.json() == {"message": "Testing operation failed.", "source": "system"}
    context[0].summarize_measurements = original
    assert http.get("/test/report?session_id=unknown", headers=headers).status_code == 404
    assert http.get("/test/report", headers=headers).status_code == 400
    context[0].sessions["x"] = (
        context[0].sessions["x"].model_copy(update={"state": SessionState.PREPARING})
    )
    assert http.get("/test/report?session_id=x", headers=headers).status_code == 409
    context[0].sessions["x"] = (
        context[0].sessions["x"].model_copy(update={"owner_subject": "foreign"})
    )
    assert http.get("/test/report?session_id=x", headers=headers).status_code == 403
    assert client(context, False).post("/test/setup").status_code == 404


def test_cleaned_report_is_not_found_but_id_stays_reserved(context):
    _, _, service, admin = context
    service.prepare("cleaned", admin, {})
    service.cleanup("cleaned", admin)
    http = client(context)
    headers = {"Authorization": "Bearer admin"}
    response = http.get("/test/report?session_id=cleaned", headers=headers)
    assert response.status_code == 404
    assert response.json()["source"] == "system"
    assert (
        http.post("/test/setup", json={"session_id": "cleaned"}, headers=headers).status_code == 409
    )


def test_invalid_stored_models_are_system_errors(context):
    from pydantic import ValidationError

    _, _, service, admin = context
    service.prepare("broken", admin, {})
    try:
        CoverageMeasure.model_validate({"covered": -1})
    except ValidationError as exc:
        context[0].summarize_measurements = Mock(side_effect=exc)
    response = client(context).get(
        "/test/report?session_id=broken", headers={"Authorization": "Bearer admin"}
    )
    assert response.status_code == 500
    assert response.json() == {"message": "Testing operation failed.", "source": "system"}


@pytest.mark.parametrize("operation", ["setup", "report"])
def test_deadline_returns_without_waiting_for_repository(context, monkeypatch, operation):
    import asyncio
    from threading import Event

    import httpx

    from app.test_interface import routes

    entered, release, finished = Event(), Event(), Event()
    _, _, service, admin = context
    service.prepare("slow", admin, {})

    def blocked(*args):
        entered.set()
        try:
            release.wait(5)
            return CoverageMeasure(covered=None, total=None, basis="Unavailable.")
        finally:
            finished.set()

    if operation == "setup":
        monkeypatch.setattr(service, "prepare", blocked)
        monkeypatch.setattr(routes, "SETUP_TIMEOUT_SECONDS", 0.05)
    else:
        monkeypatch.setattr(service, "get_code_coverage", blocked)
        monkeypatch.setattr(routes, "REPORT_TIMEOUT_SECONDS", 0.05)

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client(context).app), base_url="http://test"
        ) as http:
            headers = {"Authorization": "Bearer admin"}
            if operation == "setup":
                response = await asyncio.wait_for(
                    http.post("/test/setup", json={"session_id": "slow-new"}, headers=headers),
                    timeout=1,
                )
            else:
                response = await asyncio.wait_for(
                    http.get("/test/report?session_id=slow", headers=headers), timeout=1
                )
            assert entered.is_set()
            assert not finished.is_set()
            assert response.status_code == 500
            assert response.json()["source"] == "system"

    try:
        asyncio.run(check())
    finally:
        release.set()
        assert finished.wait(1)


def test_report_cleanup_race_returns_not_found(context):
    from app.contracts.errors import RecordNotFoundError

    repo, _, service, admin = context
    service.prepare("race", admin, {})

    def cleanup_during_aggregation(session_id):
        service.cleanup(session_id, admin)
        # Jackson's repository raises this when aggregation sees a CLEANED tombstone.
        raise RecordNotFoundError("Namespace was cleaned")

    repo.summarize_measurements = cleanup_during_aggregation
    response = client(context).get(
        "/test/report?session_id=race", headers={"Authorization": "Bearer admin"}
    )
    assert response.status_code == 404
    assert repo.sessions["race"].state == SessionState.CLEANED
