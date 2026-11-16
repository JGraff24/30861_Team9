"""Real scheduled execution attribution; never infer coverage from passed assertions."""

import json

import pytest
from fastapi.testclient import TestClient

from app.contracts.errors import IntegrityError
from app.contracts.models import ExecutionMeasurement
from app.main import create_app
from tests.ryan.test_phase5_clock import clock
from tests.unit import test_phase5_backend as backend
from tests.unit.test_phase5_backend import (
    ADMIN,
    headers,
    policy,
    prepare,
    repository,
    upload,
    worker,
)


@pytest.fixture
def client(tmp_path):
    for unused in backend.client.__wrapped__(tmp_path):
        settings = unused.app.state.services.settings.model_copy(
            update={"enable_code_measurement": True, "deployment_sha": "ryan-phase5-test"}
        )
        # The default namespace was prepared on the earlier revision; use fresh namespaces.
        with TestClient(create_app(settings)) as active:
            yield active


def rows(client, sid):
    with repository(client)._db() as db:
        return [
            json.loads(row[0])
            for row in db.execute(
                "SELECT payload FROM measurements WHERE session_id=? ORDER BY invocation_id", (sid,)
            )
        ]


def report(client, sid):
    response = client.get(f"/test/report?session_id={sid}", headers=headers("admin", None))
    assert response.status_code == 200, response.text
    return response.content


def test_scheduled_execution_scoped_measured_replay_restart_cleanup(client):
    for sid in ("measured", "untouched", "other"):
        prepare(client, sid)
    initial = report(client, "untouched")
    item = upload(client, session="measured", clean=False)
    worker(client)
    original_report = repository(client).get_report(item["auditId"])
    policy(client, item["workbookId"], {"enabled": True, "intervalDays": 1}, "measured")
    before = rows(client, "measured")
    scheduled = clock(client, "2027-03-01T00:00:00Z", "measured").json()
    assert len(scheduled["triggeredAudits"]) == 1
    # Clock acceptance itself cannot invent engine coverage.
    assert rows(client, "measured") == before
    worker(client)
    after = rows(client, "measured")
    assert len(after) > len(before)
    assert all(m["session_id"] == "measured" and m["revision"] == "ryan-phase5-test" for m in after)
    assert all(m["unavailable_reason"] is None for m in after)
    scope = after[0]["executable_lines"]
    assert all(m["executable_lines"] == scope for m in after)
    added = [m for m in after if m["invocation_id"] not in {m["invocation_id"] for m in before}]
    assert any(m["covered_lines"]["app/audit/rules_v2.py"] for m in added)
    assert repository(client).get_report(item["auditId"]) == original_report
    snapshot = report(client, "measured")
    measure = json.loads(snapshot)["coverage"]["code"]
    assert 0 < measure["covered"] <= measure["total"]
    assert "excludes API, storage, UI" in measure["basis"]
    for sid in ("other", None):
        upload(client, session=sid)
        worker(client)
    assert report(client, "measured") == snapshot
    assert report(client, "untouched") == initial
    measurement = ExecutionMeasurement.model_validate(after[0])
    repository(client).record_measurement(measurement)
    assert rows(client, "measured") == after
    with pytest.raises(IntegrityError):
        repository(client).record_measurement(measurement.model_copy(update={"revision": "wrong"}))
    with TestClient(create_app(client.app.state.services.settings)) as restarted:
        assert report(restarted, "measured") == snapshot
        assert report(restarted, "measured") == snapshot
        assert rows(restarted, "measured") == after
        restarted.app.state.services.session_service.cleanup("measured", ADMIN)
        assert rows(restarted, "measured") == []
        assert report(restarted, "untouched") == initial
        assert rows(restarted, "other")
