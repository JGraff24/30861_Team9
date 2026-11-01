"""Durable grading activity without changing session/coverage wire contracts."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import SystemSettings
from app.contracts.errors import ProjectException
from app.contracts.models import Principal, TestSession
from app.services.assembly import assemble
from app.services.projects import ProjectService
from app.storage.sqlite import SQLiteRepository
from app.test_interface.service import PersistentSessionService
from scripts.deploy_host import Deployment


def context(tmp_path: Path):
    (tmp_path / "tester").mkdir()
    with sqlite3.connect(tmp_path / "tester" / "tester.sqlite3") as db:
        db.execute("CREATE TABLE tester_sessions (state TEXT)")
    services = assemble(
        SystemSettings(
            _env_file=None,
            metadata_db_path=tmp_path / "system" / "system.sqlite3",
            blob_root=tmp_path / "blobs",
            enable_test_interface=True,
            test_session_users={"admin": ["owner", "reviewer"]},
        )
    )
    services.repository.initialize()
    services.blob_store.initialize()
    sessions = services.session_service
    assert isinstance(sessions, PersistentSessionService)
    session = sessions.prepare("grading", Principal(subject="admin", role="test_admin"), {})
    return services, sessions, session


def active_work(data):
    return Deployment(data, "JGraff24/30861_Team9", "a" * 40).active_work(data)


def stored_payload(services):
    with sqlite3.connect(services.settings.metadata_db_path) as db:
        return db.execute("SELECT payload FROM sessions WHERE id='grading'").fetchone()[0]


def test_setup_protects_external_grading_between_jobs_and_after_restart(tmp_path):
    services, _, session = context(tmp_path)
    assert session.deployment_protected_until == session.created_at + timedelta(seconds=1900)
    restarted = SQLiteRepository(services.settings.metadata_db_path)
    restarted.initialize()
    assert restarted.get_test_session("grading") == session
    assert active_work(tmp_path) == 1


@pytest.mark.parametrize("role", ["user", "reviewer"])
def test_allowed_business_activity_renews_durable_lease_without_changing_measurements(
    tmp_path, monkeypatch, role
):
    services, sessions, session = context(tmp_path)
    later = session.created_at + timedelta(seconds=101)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return later

    monkeypatch.setattr("app.services.projects.datetime", Clock)
    before_coverage = sessions.get_code_coverage(
        "grading", Principal(subject="admin", role="test_admin")
    )
    ProjectService(services).check_session(
        "grading", Principal(subject="owner" if role == "user" else "reviewer", role=role)
    )
    protected = services.repository.get_test_session("grading")
    assert protected.deployment_protected_until == later + timedelta(seconds=1900)
    assert protected.created_at == session.created_at and protected.revision == session.revision
    before = stored_payload(services)
    assert (
        sessions.get_code_coverage("grading", Principal(subject="admin", role="test_admin"))
        == before_coverage
    )
    assert stored_payload(services) == before  # Instructor report stays read-only.


def test_unknown_outsider_and_closed_sessions_cannot_extend_protection(tmp_path):
    services, sessions, session = context(tmp_path)
    before = stored_payload(services)
    for sid, subject in (("grading", "outsider"), ("missing", "owner")):
        with pytest.raises(ProjectException):
            ProjectService(services).check_session(sid, Principal(subject=subject, role="user"))
    assert stored_payload(services) == before
    future = datetime.now(UTC) + timedelta(hours=2)
    repository = services.repository
    assert not repository.renew_test_session_protection("grading", "outsider", future)
    assert not repository.renew_test_session_protection("missing", "owner", future)
    sessions.cleanup("grading", Principal(subject="admin", role="test_admin"))
    closed = stored_payload(services)
    assert not repository.renew_test_session_protection("grading", "owner", future)
    assert stored_payload(services) == closed
    assert active_work(tmp_path) == 0
    assert session.session_id == "grading"


def test_renewal_never_shortens_an_existing_lease(tmp_path):
    services, _, session = context(tmp_path)
    repository = services.repository
    future = session.created_at + timedelta(hours=1)
    assert repository.renew_test_session_protection("grading", "owner", future)
    assert repository.renew_test_session_protection("grading", "owner", session.created_at)
    assert repository.get_test_session("grading").deployment_protected_until == future


def test_legacy_session_payload_is_read_without_inventing_activity_or_rewriting(tmp_path):
    services, _, _ = context(tmp_path)
    with sqlite3.connect(services.settings.metadata_db_path) as db:
        db.execute(
            "UPDATE sessions SET payload=json_remove(payload,'$.deployment_protected_until')"
        )
    before = stored_payload(services)
    restarted = SQLiteRepository(services.settings.metadata_db_path)
    restarted.initialize()
    session = restarted.get_test_session("grading")
    assert isinstance(session, TestSession)
    assert session.deployment_protected_until is None
    assert stored_payload(services) == before


def test_frequent_polls_do_not_rewrite_session_on_every_request(tmp_path):
    services, _, _ = context(tmp_path)
    before = stored_payload(services)
    project = ProjectService(services)
    for _ in range(3):
        project.check_session("grading", Principal(subject="owner", role="user"))
    assert stored_payload(services) == before
