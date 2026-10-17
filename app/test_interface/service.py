"""Session policy and orchestration; all persistence belongs to the repository."""

from datetime import UTC, datetime

from pydantic import ValidationError

from app.config import SystemSettings
from app.contracts.errors import ProjectException, RecordNotFoundError, StorageError
from app.contracts.models import (
    CoverageMeasure,
    Principal,
    Role,
    SessionState,
    SystemSetupRequest,
    TestSession,
)
from app.contracts.ports import BlobStore, Repository


class PersistentSessionService:
    def __init__(
        self, repository: Repository, blob_store: BlobStore, settings: SystemSettings
    ) -> None:
        self.repository = repository
        self.blob_store = blob_store
        self.settings = settings

    def _owner(self, session_id: str, principal: Principal) -> TestSession:
        self._admin(principal)
        session = self.repository.get_test_session(session_id)
        if session is None:
            raise ProjectException(404, "SESSION_NOT_FOUND", "Unknown test session.")
        if session.owner_subject != principal.subject:
            raise ProjectException(
                403, "SESSION_FORBIDDEN", "Session belongs to another administrator."
            )
        return session

    def _admin(self, principal: Principal) -> None:
        if not self.settings.enable_test_interface:
            raise ProjectException(404, "TEST_DISABLED", "Testing interface is disabled.")
        if principal.role != Role.TEST_ADMIN:
            raise ProjectException(403, "TEST_ADMIN_REQUIRED", "Test administrator required.")

    def require_ready(self, session_id: str, principal: Principal) -> TestSession:
        if not self.settings.enable_test_interface:
            raise ProjectException(403, "TEST_DISABLED", "Testing interface is disabled.")
        session = self.repository.get_test_session(session_id)
        if session is None:
            raise ProjectException(404, "SESSION_NOT_FOUND", "Unknown test session.")
        if principal.subject not in session.allowed_subjects:
            raise ProjectException(
                403, "SESSION_FORBIDDEN", "User is not approved for this session."
            )
        self._ready(session)
        return session

    @staticmethod
    def _ready(session: TestSession) -> None:
        if session.state != SessionState.READY:
            raise ProjectException(409, "SESSION_NOT_READY", "Test session is not ready.")

    def cleanup(self, session_id: str, principal: Principal) -> None:
        self._owner(session_id, principal)
        keys = self.repository.begin_session_cleanup(session_id, principal.subject)
        try:
            for key in keys:
                self.blob_store.delete_test_source(key, session_id)
            self.repository.finish_session_cleanup(session_id)
        except Exception:
            self.repository.mark_session_failed(
                session_id, "Scoped cleanup incomplete; retry cleanup."
            )
            raise StorageError("Scoped cleanup failed") from None

    def prepare(
        self, session_id: str, principal: Principal, setup: dict[str, object]
    ) -> TestSession:
        self._admin(principal)
        try:
            request = SystemSetupRequest.model_validate({"session_id": session_id, "setup": setup})
        except ValidationError:
            raise ProjectException(400, "INVALID_SETUP", "Invalid setup request.") from None
        cleanup = request.setup.get("cleanup_session_ids", [])
        assert isinstance(cleanup, list)
        if session_id in cleanup:
            raise ProjectException(400, "INVALID_SETUP", "Cannot clean the new session.")
        # Check ownership before any cleanup, then atomically reserve the new ID.
        for old in cleanup:
            assert isinstance(old, str)
            self._owner(old, principal)
        session = TestSession(
            session_id=session_id,
            owner_subject=principal.subject,
            allowed_subjects=list(self.settings.test_session_users.get(principal.subject, [])),
            state=SessionState.PREPARING,
            created_at=datetime.now(UTC),
            revision=self.settings.deployment_sha,
            cleanup_error=None,
        )
        self.repository.create_test_session(session)
        try:
            for old in cleanup:
                assert isinstance(old, str)
                self.cleanup(old, principal)
            if not self.blob_store.health():
                raise StorageError("Source store unavailable")
            self.repository.mark_session_ready(session_id)
        except Exception:
            self.repository.mark_session_failed(session_id, "Session preparation failed.")
            raise
        return session.model_copy(update={"state": SessionState.READY})

    def get_code_coverage(self, session_id: str, principal: Principal) -> CoverageMeasure:
        session = self._owner(session_id, principal)
        if session.state == SessionState.CLEANED:
            raise ProjectException(404, "SESSION_NOT_FOUND", "Test session was cleaned.")
        self._ready(session)
        try:
            return self.repository.summarize_measurements(session_id)
        except RecordNotFoundError:
            # Cleanup can reserve CLEANED after the readiness read but before aggregation.
            raise ProjectException(404, "SESSION_NOT_FOUND", "Test session was cleaned.") from None
