"""Backend scheduling, isolated-clock and recoverable owner-deletion services."""

import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from app.contracts.errors import ProjectException, StorageError
from app.contracts.models import Principal, Role, SessionState
from app.contracts.phase5 import (
    ClockRequest,
    ClockResponse,
    NotificationResponse,
    ScheduleRequest,
    ScheduleResponse,
    timestamp,
)
from app.contracts.ports import SystemServices
from app.services.projects import ProjectService
from app.storage.blobs import FilesystemBlobStore
from app.storage.locks import workbook_storage_lock
from app.storage.sqlite import SQLiteRepository

logger = logging.getLogger(__name__)


class LifecycleService:
    def __init__(self, services: SystemServices) -> None:
        self.services = services
        self.projects = ProjectService(services)

    @property
    def repository(self) -> SQLiteRepository:
        repository = self.services.repository
        if not isinstance(repository, SQLiteRepository):
            raise ProjectException(
                503, "SERVICE_UNAVAILABLE", "Lifecycle persistence is unavailable."
            )
        return repository

    def get_schedule(
        self, wid: str, principal: Principal, session_id: str | None
    ) -> ScheduleResponse:
        self.projects.authorize_workbook(wid, principal, session_id)
        return self.repository.get_schedule(wid)

    def configure_schedule(
        self, wid: str, principal: Principal, session_id: str | None, body: object
    ) -> ScheduleResponse:
        self.projects.authorize_workbook(wid, principal, session_id, True)
        try:
            request = ScheduleRequest.model_validate(body)
        except ValidationError:
            raise ProjectException(400, "INVALID_REQUEST", "Schedule fields are invalid.") from None
        if request.enabled:
            self.projects._require_ruleset()
        return self.repository.configure_schedule(wid, request)

    def get_notifications(
        self, wid: str, principal: Principal, session_id: str | None
    ) -> NotificationResponse:
        self.projects.authorize_workbook(wid, principal, session_id)
        return self.repository.get_notifications(wid)

    def authorize_clock(self, principal: Principal, session_id: str | None) -> str:
        if not self.services.settings.enable_test_clock or session_id is None:
            raise ProjectException(404, "TEST_CLOCK_DISABLED", "Isolated clock is unavailable.")
        if principal.role != Role.TEST_ADMIN:
            raise ProjectException(403, "FORBIDDEN", "Test administrator required.")
        session = self.repository.get_test_session(session_id)
        if session is None or session.state == SessionState.CLEANED:
            raise ProjectException(400, "INVALID_TEST_SESSION", "Test session is unknown.")
        if session.owner_subject != principal.subject:
            raise ProjectException(403, "FORBIDDEN", "Session belongs to another administrator.")
        if session.state != SessionState.READY:
            raise ProjectException(409, "SESSION_NOT_READY", "Test session is not ready.")
        if session.clock_current_time is None:
            raise ProjectException(409, "LEGACY_SESSION_CLOCK", "Prepare a fresh test session.")
        return session_id

    def advance_clock(
        self, principal: Principal, session_id: str | None, body: object
    ) -> ClockResponse:
        sid = self.authorize_clock(principal, session_id)
        try:
            request = ClockRequest.model_validate(body)
        except ValidationError:
            raise ProjectException(400, "INVALID_REQUEST", "Clock fields are invalid.") from None
        self.projects._require_ruleset()
        return self.repository.process_schedules(
            timestamp(request.currentTime),
            session_id=sid,
            owner_subject=principal.subject,
            ruleset_version=self.services.settings.ruleset_version,
            queue_seconds=self.services.settings.queue_timeout_seconds,
        )

    def tick(self) -> ClockResponse:
        self.projects._require_ruleset()
        return self.repository.process_schedules(
            datetime.now(UTC),
            ruleset_version=self.services.settings.ruleset_version,
            queue_seconds=self.services.settings.queue_timeout_seconds,
        )

    def delete_workbook(self, wid: str, principal: Principal, session_id: str | None) -> None:
        self.projects.check_session(session_id, principal)
        pending = self.repository.deletion_owner(wid)
        if pending is not None:
            # Pending cleanup is visible only through the original owner's DELETE.
            if pending != (principal.subject, session_id) or principal.role != Role.USER:
                raise ProjectException(404, "NOT_FOUND", "Workbook is unavailable.")
        else:
            self.projects.authorize_workbook(wid, principal, session_id, True)
        started = time.monotonic()
        with workbook_storage_lock(self.services.settings.metadata_db_path, wid):
            keys = self.repository.begin_workbook_deletion(wid, principal.subject, session_id)
            if keys == ["TEST_DELETION_FAILURE"]:
                raise ProjectException(
                    500,
                    "TEST_DELETION_FAILURE",
                    "Deletion failure requested by the isolated test session.",
                )
            try:
                blobs = self.services.blob_store
                if blobs is None:
                    raise StorageError("Blob service unavailable")
                # Independent content-free journal is carried forward on every restore.
                self.repository.record_deletion(wid)
                stores = [
                    blobs,
                    *(
                        FilesystemBlobStore(path)
                        for path in self.services.settings.managed_blob_backups
                    ),
                ]
                for store in stores:
                    store.delete_workbook_sources(wid, session_id, started + 25)
                backups = list(self.services.settings.managed_metadata_backups)
                root = self.services.settings.managed_metadata_backup_root
                if root is not None:
                    if not root.is_dir():
                        raise StorageError("Registered backup root unavailable")
                    backups.extend(root.glob("*/system.sqlite3"))
                for path in backups:
                    self._purge_backup(path, wid, keys, started + 25)
                if time.monotonic() - started >= 25:
                    raise StorageError("Deletion cleanup deadline exceeded")
                self.repository.finish_workbook_deletion(wid)
            except Exception:
                logger.error(
                    "workbook_deletion_failed", extra={"workbookId": wid, "stage": "deletion"}
                )
                raise ProjectException(
                    500,
                    "DELETION_FAILED",
                    "Workbook cleanup is incomplete; the owner may retry deletion.",
                ) from None
        logger.info("workbook_deleted", extra={"workbookId": wid, "stage": "deletion"})

    @staticmethod
    def _purge_backup(path: Path, wid: str, keys: list[str], deadline: float) -> None:
        if not path.is_file() or time.monotonic() >= deadline:
            raise StorageError("Registered backup is unavailable")
        backup = SQLiteRepository(path)
        backup.initialize()
        with backup._db(write=True) as db:
            workbook = db.execute("SELECT session_id FROM workbooks WHERE id=?", (wid,)).fetchone()
            sid = workbook[0] if workbook else None
            for key in set(keys + backup._workbook_blob_keys(db, wid, sid)):
                db.execute("DELETE FROM upload_staging WHERE blob_key=?", (key,))
                db.execute("DELETE FROM retired_blobs WHERE blob_key=?", (key,))
            backup._erase_workbook(db, wid)
            db.execute("DELETE FROM deletions WHERE workbook_id=?", (wid,))
        backup.compact_deleted_content()

    def recover_deletions(self) -> None:
        with self.repository._db() as db:
            pending = db.execute(
                "SELECT workbook_id,owner,session_id FROM deletions LIMIT 1"
            ).fetchall()
        for wid, owner, sid in pending:
            try:
                self.delete_workbook(wid, Principal(subject=owner, role=Role.USER), sid)
            except (ProjectException, StorageError):
                logger.error("deletion_recovery_pending", extra={"workbookId": wid})
