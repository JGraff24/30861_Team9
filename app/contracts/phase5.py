"""Phase 5 business DTOs and strict calendar arithmetic, without HTTP dependencies."""

import re
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, StrictInt, field_validator, model_validator

from app.contracts.errors import ProjectException
from app.contracts.models import ContractModel, Identifier, UTCDateTime

FAULT_KEYS = frozenset(
    {
        "audit_failures",
        "remediation_failures",
        "report_failures",
        "storage_failures",
        "scheduled_audit_failures",
        "notification_failures",
        "deletion_failures",
    }
)


def timestamp(value: object) -> datetime:
    if (
        not isinstance(value, str)
        or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z", value
        )
        is None
    ):
        raise ValueError("UTC RFC3339 timestamp required")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def add_days(value: datetime, days: int) -> datetime:
    try:
        return value + timedelta(days=days)
    except OverflowError:
        raise ProjectException(
            400, "TIME_RANGE_EXCEEDED", "Calendar calculation is out of range."
        ) from None


def reminder_time(deadline: str, offset: int) -> datetime | None:
    try:
        return datetime.combine(date.fromisoformat(deadline), datetime.min.time(), UTC) - timedelta(
            days=offset
        )
    except OverflowError:
        return None


class ScheduleRequest(ContractModel):
    enabled: StrictBool
    intervalDays: Annotated[StrictInt, Field(ge=1, le=90)] | None = None
    complianceDeadline: str | None = None

    @field_validator("complianceDeadline")
    @classmethod
    def calendar_date(cls, value: str | None) -> str | None:
        if value is not None:
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
                raise ValueError("Calendar date required")
            date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def interval_required(self) -> Self:
        if self.enabled and self.intervalDays is None:
            raise ValueError("Enabled schedules require an interval")
        return self


class ScheduleResponse(ContractModel):
    workbookId: Identifier
    enabled: bool
    intervalDays: int | None
    complianceDeadline: str | None
    versionId: Identifier
    nextAuditAt: UTCDateTime | None
    reminderDays: tuple[Literal[90], Literal[30], Literal[7]]


class Notification(ContractModel):
    notificationId: Identifier
    type: Literal["COMPLIANCE_DEADLINE_REMINDER"] = "COMPLIANCE_DEADLINE_REMINDER"
    workbookId: Identifier
    versionId: Identifier
    scheduledFor: UTCDateTime
    status: Literal["DELIVERED", "FAILED"]
    message: str


class NotificationResponse(ContractModel):
    notifications: list[Notification]


class ClockRequest(ContractModel):
    currentTime: str

    @field_validator("currentTime")
    @classmethod
    def utc_timestamp(cls, value: str) -> str:
        timestamp(value)
        return value


class ClockResponse(ContractModel):
    currentTime: UTCDateTime
    triggeredAudits: list[Identifier]
    triggeredNotifications: list[Identifier]
