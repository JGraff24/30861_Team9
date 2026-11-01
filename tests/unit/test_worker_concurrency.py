"""Real SQLite/blob/worker-process concurrency; only engine timing is controlled.

The test engine runs the installed OOXML validator and all six real audit rules.
Its optional caller-owned file gate holds one audit so claim order, slot capacity,
cleanup progress and shutdown can be observed without changing production rules.
"""

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest

from app.audit.engine import OoxmlWorkbookEngine
from app.config import SystemSettings
from app.contracts.models import (
    AuditEvaluation,
    AuditRecord,
    JobRecord,
    ProcessingLimits,
    TestSession,
    ValidatedWorkbook,
    VersionRecord,
    WorkbookRecord,
)
from app.jobs.worker import AuditWorker, run_workers
from app.services.assembly import assemble
from app.storage.blobs import FilesystemBlobStore, sha256
from app.storage.sqlite import SQLiteRepository
from tests.unit.phase4_builder import Sheet, Workbook


class ControlledAuditEngine(OoxmlWorkbookEngine):
    """Real engine with test-only scheduling gates, never selected in production."""

    def __init__(self) -> None:
        super().__init__()
        self.gate_root = Path(os.environ["WORKER_TEST_GATE_ROOT"])

    def audit(
        self, data: bytes, validated: ValidatedWorkbook, ruleset_version: str
    ) -> AuditEvaluation:
        slow = next((name for name in validated.worksheet_parts if name.startswith("Slow")), None)
        if slow is not None:
            (self.gate_root / f"started-{slow}").write_text("started")
            deadline = time.monotonic() + 15
            while not (self.gate_root / f"release-{slow}").exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError("The test did not release its audit timing gate")
                time.sleep(0.02)
        return super().audit(data, validated, ruleset_version)


def state(tmp_path: Path) -> tuple[SQLiteRepository, FilesystemBlobStore, Path]:
    repository = SQLiteRepository(tmp_path / "metadata.sqlite3")
    repository.initialize()
    blobs = FilesystemBlobStore(tmp_path / "blobs")
    blobs.initialize()
    gates = tmp_path / "gates"
    gates.mkdir()
    return repository, blobs, gates


def enqueue(
    repository: SQLiteRepository, blobs: FilesystemBlobStore, suffix: str, sheet: str
) -> JobRecord:
    now = datetime.now(UTC)
    session_id = f"session-{suffix}"
    repository.create_test_session(
        TestSession(
            session_id=session_id,
            owner_subject="administrator",
            allowed_subjects=["owner"],
            state="PREPARING",
            created_at=now,
            revision="unknown",
            cleanup_error=None,
        )
    )
    repository.mark_session_ready(session_id)
    data = Workbook([Sheet(sheet, {"A1": "Content"})]).build()
    validated = OoxmlWorkbookEngine().validate(data, "source.xlsx", ProcessingLimits())
    wid, vid, aid, jid = (
        f"{prefix}-{suffix}" for prefix in ("workbook", "version", "audit", "job")
    )
    key = f"tests/{session_id}/{wid}/{vid}/source"
    blobs.put_source(key, data)
    job = JobRecord(
        job_id=jid,
        audit_id=aid,
        workbook_id=wid,
        version_id=vid,
        session_id=session_id,
        state="QUEUED",
        created_at=now,
        queue_deadline=now + timedelta(seconds=15),
        worker_id=None,
        lease_expires_at=None,
    )
    repository.create_upload(
        WorkbookRecord(
            workbook_id=wid, owner_subject="owner", session_id=session_id, created_at=now
        ),
        VersionRecord(
            version_id=vid,
            workbook_id=wid,
            owner_subject="owner",
            session_id=session_id,
            file_name="source.xlsx",
            source_sha256=validated.source_sha256,
            blob_key=key,
            size_bytes=len(data),
            worksheet_count=validated.worksheet_count,
            nonempty_cell_count=validated.nonempty_cell_count,
            created_at=now,
            status="PROCESSING",
            latest_audit_id=aid,
            source_version_id=None,
            validated=validated,
        ),
        AuditRecord(
            audit_id=aid,
            workbook_id=wid,
            version_id=vid,
            owner_subject="owner",
            session_id=session_id,
            ruleset_version="2.0.0",
            status="PROCESSING",
            started_at=now,
            completed_at=None,
            findings=[],
            limitations=[],
            error=None,
        ),
        job,
    )
    return job


def environment(
    repository: SQLiteRepository, blobs: FilesystemBlobStore, gates: Path
) -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "RULESET_VERSION": "2.0.0",
        "METADATA_DB_PATH": str(repository.path),
        "BLOB_ROOT": str(blobs.root),
        "ENGINE_FACTORY": "tests.unit.test_worker_concurrency:ControlledAuditEngine",
        "WORKER_TEST_GATE_ROOT": str(gates),
        "PRIVATE_TEST_SECRET": "must-never-appear-in-worker-logs",
    }


def launch(
    repository: SQLiteRepository,
    blobs: FilesystemBlobStore,
    gates: Path,
    *arguments: str,
) -> tuple[subprocess.Popen[bytes], Path]:
    logs = gates.parent / "worker.stderr"
    with logs.open("ab") as handle:
        process = subprocess.Popen(
            [sys.executable, "-m", "app.jobs.worker", *arguments],
            cwd=gates.parent,
            env=environment(repository, blobs, gates),
            stdout=subprocess.DEVNULL,
            stderr=handle,
        )
    return process, logs


def eventually(condition: Callable[[], bool], process: subprocess.Popen[bytes], logs: Path) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        assert process.poll() is None, logs.read_text()
        if condition():
            return
        time.sleep(0.03)
    raise AssertionError("Worker did not reach the expected durable state within eight seconds")


def release(gates: Path, *names: str) -> None:
    for name in names:
        (gates / f"release-{name}").touch()


def terminate(process: subprocess.Popen[bytes], gates: Path) -> None:
    release(gates, "SlowOne", "SlowTwo")
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_two_slots_execute_an_independent_namespace_before_a_slow_audit_finishes(
    tmp_path: Path,
) -> None:
    repository, blobs, gates = state(tmp_path)
    slow = enqueue(repository, blobs, "slow", "SlowOne")
    process, logs = launch(repository, blobs, gates, "--concurrency", "2")
    try:
        eventually(lambda: (gates / "started-SlowOne").exists(), process, logs)
        small = enqueue(repository, blobs, "small", "Report")
        eventually(lambda: repository.get_job(small.job_id).state == "DONE", process, logs)
        slow_running = repository.get_job(slow.job_id)
        small_done = repository.get_job(small.job_id)
        assert slow_running.state == "RUNNING"
        assert slow_running.worker_id != small_done.worker_id
        assert slow_running.lease_expires_at is not None
        assert small_done.lease_expires_at is not None
        output = repository.get_audit(small.workbook_id, small.version_id, small.audit_id)
        assert output.status == "AUDIT_COMPLETE"
        assert output.checks_performed == [
            "ALT_TEXT",
            "WORKSHEET_NAME",
            "TABLE_HEADER",
            "MERGED_CELLS",
            "LINK_TEXT",
            "COLOR_CONTRAST",
        ]
        release(gates, "SlowOne")
        eventually(lambda: repository.get_job(slow.job_id).state == "DONE", process, logs)
    finally:
        terminate(process, gates)
    assert process.returncode == 0
    assert "must-never-appear-in-worker-logs" not in logs.read_text()


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
def test_signal_stops_claims_drains_two_jobs_and_housekeeping_progresses_under_load(
    tmp_path: Path, stop_signal: signal.Signals
) -> None:
    repository, blobs, gates = state(tmp_path)
    first = enqueue(repository, blobs, "first", "SlowOne")
    second = enqueue(repository, blobs, "second", "SlowTwo")
    process, logs = launch(repository, blobs, gates, "--concurrency", "2")
    try:
        eventually(
            lambda: all((gates / f"started-{name}").exists() for name in ("SlowOne", "SlowTwo")),
            process,
            logs,
        )
        assert repository.get_job(first.job_id).state == "RUNNING"
        assert repository.get_job(second.job_id).state == "RUNNING"
        key, data = "ordinary/retired-workbook/retired-version/source", b"retired-candidate"
        blobs.put_source(key, data)
        with repository._db(write=True) as db:
            db.execute("INSERT INTO retired_blobs VALUES (?,?)", (key, sha256(data)))
        eventually(lambda: not (blobs.root / key).exists(), process, logs)
        queued = enqueue(repository, blobs, "queued", "Report")
        process.send_signal(stop_signal)
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=0.15)
        assert repository.get_job(queued.job_id).state == "QUEUED"
        release(gates, "SlowOne", "SlowTwo")
        assert process.wait(timeout=10) == 0
        assert repository.get_job(first.job_id).state == "DONE"
        assert repository.get_job(second.job_id).state == "DONE"
        assert repository.get_job(queued.job_id).state == "QUEUED"
        events = [
            json.loads(line) for line in logs.read_text().splitlines() if line.startswith("{")
        ]
        assert any(event["event"] == "worker_stopping" for event in events)
    finally:
        terminate(process, gates)


def test_unexpected_claim_failure_stops_the_process_instead_of_losing_a_slot(
    tmp_path: Path,
) -> None:
    repository, blobs, gates = state(tmp_path)
    enqueue(repository, blobs, "failure", "Report")
    with repository._db(write=True) as db:
        db.execute(
            "CREATE TRIGGER reject_claim BEFORE UPDATE ON jobs WHEN NEW.state='RUNNING' "
            "BEGIN SELECT RAISE(ABORT,'controlled claim failure'); END"
        )
    process, logs = launch(repository, blobs, gates, "--concurrency", "2")
    try:
        assert process.wait(timeout=10) != 0
        events = [
            json.loads(line) for line in logs.read_text().splitlines() if line.startswith("{")
        ]
        assert any(
            event["event"] == "worker_slot_failed" and event["level"] == "ERROR" for event in events
        )
        assert "must-never-appear-in-worker-logs" not in logs.read_text()
    finally:
        terminate(process, gates)


@pytest.mark.parametrize("arguments", [("--once", "--concurrency", "2"), ("--concurrency", "3")])
def test_cli_rejects_unbounded_or_ambiguous_once_concurrency(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    repository, blobs, gates = state(tmp_path)
    process, logs = launch(repository, blobs, gates, *arguments)
    try:
        assert process.wait(timeout=10) == 2
        assert "error:" in logs.read_text()
    finally:
        terminate(process, gates)


def test_housekeeping_budget_advances_fairly_through_real_durable_intents(tmp_path: Path) -> None:
    repository, blobs, _ = state(tmp_path)
    keys = [f"ordinary/workbook-{number}/version-{number}/source" for number in range(3)]
    for key in keys:
        data = key.encode()
        blobs.put_source(key, data)
        with repository._db(write=True) as db:
            db.execute("INSERT INTO retired_blobs VALUES (?,?)", (key, sha256(data)))
    services = assemble(SystemSettings(_env_file=None), repository=repository, blob_store=blobs)
    worker = AuditWorker(services)
    for expected in range(1, 4):
        worker._cleanup_candidates(max_items=1)
        assert sum(not (blobs.root / key).exists() for key in keys) == expected
    assert len(repository.pending_blob_cleanup()) == 3


def test_unexpected_housekeeping_failure_stops_all_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    repository, blobs, _ = state(tmp_path)
    key, data = "ordinary/retired-workbook/retired-version/source", b"retired"
    blobs.put_source(key, data)
    with repository._db(write=True) as db:
        db.execute("INSERT INTO retired_blobs VALUES (?,?)", (key, sha256(data)))
    services = assemble(SystemSettings(_env_file=None), repository=repository, blob_store=blobs)

    def unexpected(_key: str, _digest: str) -> None:
        raise RuntimeError("private-cleanup-details")

    monkeypatch.setattr(blobs, "delete_uncommitted_source", unexpected)
    stopped = Event()
    with pytest.raises(RuntimeError, match="housekeeping stopped unexpectedly"):
        run_workers(services, 2, stopped)
    assert stopped.is_set()
    assert "worker_housekeeper_failed" in caplog.text
    assert "private-cleanup-details" not in caplog.text
