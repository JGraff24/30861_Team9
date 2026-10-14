"""SQLite tester sessions and immutable reports, separate from all system storage."""

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from app.contracts.models import ErrorSource, TestResult
from tester.errors import TesterError


class TesterSessionState(StrEnum):
    PREPARING = "PREPARING"
    READY = "READY"
    SETUP_FAILED = "SETUP_FAILED"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    ABORTED = "ABORTED"


ENDED = {TesterSessionState.COMPLETE, TesterSessionState.ABORTED}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tester_sessions (
    session_id TEXT PRIMARY KEY,
    operator_subject TEXT NOT NULL,
    target_url TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    results TEXT NOT NULL DEFAULT '[]',
    report BLOB,
    cleanup_pending INTEGER NOT NULL DEFAULT 0
)
"""


@dataclass(frozen=True)
class SessionRow:
    session_id: str
    operator_subject: str
    target_url: str
    state: TesterSessionState
    results: list[TestResult]
    report: bytes | None


class TesterStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._initialized = False
        self._lock = threading.Lock()

    def exists(self) -> bool:
        return self.path.exists()

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if not self._initialized:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(self.path) as setup:
                    setup.execute("PRAGMA journal_mode=WAL")
                    setup.execute(SCHEMA)
                self._initialized = True
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            # IMMEDIATE takes the write lock up front so check-then-update is atomic.
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        finally:
            connection.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> SessionRow:
        return SessionRow(
            session_id=row["session_id"],
            operator_subject=row["operator_subject"],
            target_url=row["target_url"],
            state=TesterSessionState(row["state"]),
            results=[TestResult.model_validate(item) for item in json.loads(row["results"])],
            report=row["report"],
        )

    def create(self, session_id: str, operator: str, target_url: str, now: datetime) -> None:
        with self._connect(write=True) as db:
            db.execute(
                "INSERT INTO tester_sessions (session_id, operator_subject, target_url, state,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, operator, target_url, TesterSessionState.PREPARING, now.isoformat()),
            )

    def finish_setup(self, session_id: str, ready: bool) -> None:
        state = TesterSessionState.READY if ready else TesterSessionState.SETUP_FAILED
        with self._connect(write=True) as db:
            db.execute(
                "UPDATE tester_sessions SET state = ?, cleanup_pending = ?"
                " WHERE session_id = ? AND state = ?",
                (state, 0 if ready else 1, session_id, TesterSessionState.PREPARING),
            )

    def get(self, session_id: str) -> SessionRow | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM tester_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return None if row is None else self._row(row)

    def require_owned(self, session_id: str, operator: str) -> SessionRow:
        session = self.get(session_id)
        if session is None:
            raise TesterError(404, "Unknown tester session.", ErrorSource.TESTER)
        if session.operator_subject != operator:
            raise TesterError(403, "Session belongs to another operator.", ErrorSource.TESTER)
        return session

    def begin_run(self, session_id: str, operator: str) -> SessionRow:
        """Consume a READY session exactly once; every later or concurrent run gets 409."""
        with self._connect(write=True) as db:
            row = db.execute(
                "SELECT * FROM tester_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise TesterError(404, "Unknown tester session.", ErrorSource.TESTER)
            if row["operator_subject"] != operator:
                raise TesterError(403, "Session belongs to another operator.", ErrorSource.TESTER)
            if row["state"] != TesterSessionState.READY:
                raise TesterError(
                    409, "Session is not ready; each session runs once.", ErrorSource.TESTER
                )
            db.execute(
                "UPDATE tester_sessions SET state = ? WHERE session_id = ?",
                (TesterSessionState.RUNNING, session_id),
            )
        return self._row(row)

    def save_progress(self, session_id: str, results: list[TestResult]) -> None:
        payload = json.dumps([item.model_dump(mode="json") for item in results])
        with self._connect(write=True) as db:
            db.execute(
                "UPDATE tester_sessions SET results = ? WHERE session_id = ? AND state = ?",
                (payload, session_id, TesterSessionState.RUNNING),
            )

    def finish_run(self, session_id: str, report: bytes, aborted: bool) -> None:
        state = TesterSessionState.ABORTED if aborted else TesterSessionState.COMPLETE
        with self._connect(write=True) as db:
            changed = db.execute(
                "UPDATE tester_sessions SET state = ?, report = ? WHERE session_id = ?"
                " AND state = ? AND report IS NULL",
                (state, report, session_id, TesterSessionState.RUNNING),
            ).rowcount
        if changed != 1:
            raise RuntimeError("final reports are written once by the running suite")

    def running_sessions(self) -> list[SessionRow]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM tester_sessions WHERE state = ?", (TesterSessionState.RUNNING,)
            ).fetchall()
        return [self._row(row) for row in rows]

    def mark_retrieved(self, session_id: str) -> None:
        with self._connect(write=True) as db:
            db.execute(
                "UPDATE tester_sessions SET cleanup_pending = 1 WHERE session_id = ?",
                (session_id,),
            )

    def cleanup_candidates(self, target_url: str) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT session_id FROM tester_sessions WHERE target_url = ?"
                " AND cleanup_pending = 1 ORDER BY created_at",
                (target_url,),
            ).fetchall()
        return [row["session_id"] for row in rows]

    def clear_cleanup(self, session_ids: list[str]) -> None:
        with self._connect(write=True) as db:
            db.executemany(
                "UPDATE tester_sessions SET cleanup_pending = 0 WHERE session_id = ?",
                [(session_id,) for session_id in session_ids],
            )
