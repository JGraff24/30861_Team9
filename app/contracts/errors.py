"""Typed failures; HTTP handling and request IDs belong to future web integration."""

from pydantic import JsonValue

from app.contracts.models import ExecutionErrorKind, ExecutionMeasurement, ProjectError


class ProjectException(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, JsonValue] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details if details is not None else {}

    def to_envelope(self, request_id: str) -> ProjectError:
        return ProjectError(
            code=self.code,
            message=self.message,
            requestId=request_id,
            details=self.details,
        )


class WorkbookValidationError(ProjectException):
    """Rejected intake; retains status/code/message/details, never an HTTP response."""


class EngineExecutionError(ProjectException):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, JsonValue] | None = None,
        *,
        kind: ExecutionErrorKind,
        measurement: ExecutionMeasurement | None = None,
    ) -> None:
        super().__init__(status_code, code, message, details)
        self.kind = kind
        self.measurement = measurement


class StorageError(Exception):
    """Adapter failure mapped to HTTP only after service authorization."""


class IntegrityError(StorageError):
    """Stored bytes or relational invariants differ from the committed source."""


class DuplicateIdentifierError(StorageError):
    """A record/session ID already exists, including CLEANED tombstones."""


class SessionBusyError(StorageError):
    """Active jobs prevent cleanup or a session transition."""


class StorageUnavailableError(StorageError):
    """Storage could not be reached or used."""


class RecordNotFoundError(StorageError):
    """A mutation requires a record that does not exist; reads return None."""


class LeaseConflictError(StorageError):
    """A foreign/expired lease or terminal transition prevents mutation."""
