"""Test-only fake Phase 5 target (specification 3.0.0) for exercising the independent tester.

It extends the Phase 4 fake with schedules, notifications, isolated clocks, owner deletion
and the seven injected faults. It lives under tests/ and must never be deployed or registered.
Like the Phase 4 fake, its audit returns the hand-written expectations for known fixture
hashes, so it can only prove tester behavior, never system correctness. Defect switches let
unit tests show the tester notices wrong behavior.

Audits and remediations advance only when polled, which makes "still active" windows
deterministic: an accepted audit stays PROCESSING until the configured number of polls.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from tests.support.fake_phase4 import (
    FIXTURES,
    Audit,
    FakePhase4,
    Remediation,
    Version,
    Workbook,
    project_error,
    test_error,
)
from tests.support.fake_phase4 import (
    TOKENS as PHASE4_TOKENS,
)

TOKENS = {**PHASE4_TOKENS, "test-adminB": "fake-admin-b"}
EXPECTATIONS = FIXTURES / "expectations-phase5.json"
DEFAULT_CLOCK = datetime(2027, 1, 1, tzinfo=UTC)
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(\d{1,6}))?Z")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
FAULT_KEYS = {
    "audit_failures",
    "remediation_failures",
    "report_failures",
    "storage_failures",
    "scheduled_audit_failures",
    "notification_failures",
    "deletion_failures",
}
ADMINS = {"test-admin", "test-adminB"}
ALL_CHECKS = [
    "ALT_TEXT",
    "WORKSHEET_NAME",
    "TABLE_HEADER",
    "MERGED_CELLS",
    "LINK_TEXT",
    "COLOR_CONTRAST",
]
REMINDER_MESSAGE = (
    "This stored workbook version has unresolved accessibility findings or no completed "
    "successful audit. External workbook changes require a new upload."
)


def access_map() -> dict[str, dict[str, str]]:
    from tests.support.fake_phase4 import BASE_URL

    return {BASE_URL: dict(TOKENS)}


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not TIMESTAMP.fullmatch(value):
        return None
    try:
        moment = datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    fraction = TIMESTAMP.fullmatch(value).group(1)  # type: ignore[union-attr]
    return moment.replace(microsecond=int(fraction.ljust(6, "0"))) if fraction else moment


def show(moment: datetime) -> str:
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        text += f".{moment.microsecond:06d}"
    return text + "Z"


def parse_date(value: object) -> date | None:
    if not isinstance(value, str) or not DATE.fullmatch(value):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def strict_json(content: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        if len({key for key, _ in items}) != len(items):
            raise project_error(400, "INVALID_REQUEST")
        return dict(items)

    try:
        return json.loads(content.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise project_error(400, "INVALID_REQUEST") from None


@dataclass
class Schedule:
    enabled: bool = False
    interval: int | None = None
    deadline: str | None = None
    next_at: datetime | None = None
    deadline_configured_at: datetime | None = None


@dataclass
class FakePhase5(FakePhase4):
    clock_switch: bool = True
    large_polls: int = 3  # boundary-sized uploads stay PROCESSING for this many polls
    schedules: dict[str, Schedule] = field(default_factory=dict)
    notifications: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    considered: set[tuple[str, str, int]] = field(default_factory=set)
    injected_remediations: set[str] = field(default_factory=set)
    ghosts: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Defect switches.
    reset_cadence_to_now: bool = False
    remind_clean: bool = False
    no_dedup: bool = False
    clock_ignores_busy: bool = False
    accept_interval_zero: bool = False
    delete_keeps_schedule: bool = False
    reviewer_can_delete: bool = False
    scheduled_internal_failure: bool = False
    _namespace: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        raw = json.loads(EXPECTATIONS.read_text(encoding="utf-8"))
        self.expected = {
            fixture["sha256"]: fixture
            for fixture in raw["fixtures"].values()
            if fixture["kind"] != "pending"
        }
        self.subjects = {token: name for name, token in TOKENS.items()}

    # --- time ---------------------------------------------------------------------------

    def effective(self, namespace: str | None) -> datetime | None:
        session = self.sessions.get(namespace or "")
        return session["clock"] if session else None

    def now(self) -> str:
        moment = self.effective(self._namespace)
        return show(moment) if moment is not None else super().now()

    def handle(self, request: httpx.Request) -> httpx.Response:
        self._namespace = request.headers.get("x-test-session")
        return super().handle(request)

    # --- routing ------------------------------------------------------------------------

    def route(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        parts = path.strip("/").split("/")
        if parts == ["api", "v1", "test", "clock"]:
            if request.method != "POST":
                raise project_error(404, "NOT_FOUND")
            return self.clock(request)
        phase5 = (
            len(parts) == 5
            and parts[:3] == ["api", "v1", "workbooks"]
            and parts[4] in ("schedule", "notifications")
        ) or (
            len(parts) == 4
            and parts[:3] == ["api", "v1", "workbooks"]
            and request.method == "DELETE"
        )
        if not phase5:
            return super().route(request)
        namespace = request.headers.get("x-test-session")
        if namespace is not None and namespace not in self.sessions:
            raise project_error(400, "INVALID_TEST_SESSION")
        subject = self.subject(request)
        if subject is None:
            raise project_error(401, "UNAUTHORIZED")
        if subject in ADMINS:
            raise project_error(403, "FORBIDDEN")
        if parts[3] in self.ghosts and parts[3] not in self.workbooks and parts[-1] == "schedule":
            return httpx.Response(200, json=self.ghosts[parts[3]])
        workbook = self.workbook(parts[3], namespace)
        if len(parts) == 4:
            if not (self.reviewer_can_delete and subject == "reviewer"):
                self.authorize(workbook, subject, write=True)
            return self.delete(workbook, namespace)
        if parts[4] == "notifications" and request.method == "GET":
            self.authorize(workbook, subject, write=False)
            return httpx.Response(
                200, json={"notifications": self.notifications.get(workbook.workbook_id, [])}
            )
        if parts[4] == "schedule" and request.method == "GET":
            self.authorize(workbook, subject, write=False)
            return httpx.Response(200, json=self.schedule_body(workbook))
        if parts[4] == "schedule" and request.method == "PUT":
            self.authorize(workbook, subject, write=True)
            return self.configure(request, workbook, namespace)
        raise project_error(404, "NOT_FOUND")

    # --- instructor setup ---------------------------------------------------------------

    def instructor(self, request: httpx.Request, path: str) -> httpx.Response:
        subject = self.subject(request)
        if subject is None:
            raise test_error(401, "valid test-admin credentials are required")
        if subject not in ADMINS:
            raise test_error(403, "test-admin role required")
        if path != "/test/setup" or request.method != "POST":
            return super().instructor(request, path)
        body = json.loads(request.content)
        session_id, setup = body.get("session_id"), body.get("setup", {})
        if not isinstance(session_id, str) or not isinstance(setup, dict):
            raise test_error(400, "invalid setup request")
        if set(setup) - {"cleanup_session_ids", "faults", "clock"}:
            raise test_error(400, "unknown setup key")
        faults = setup.get("faults")
        if "faults" in setup:
            if (
                not isinstance(faults, dict)
                or not faults
                or set(faults) - FAULT_KEYS
                or any(type(value) is not int or value != 1 for value in faults.values())
                or {"audit_failures", "report_failures"} <= set(faults)
            ):
                raise test_error(400, "invalid fault configuration")
            if not self.fault_switch:
                raise test_error(403, "the trusted fault switch is disabled")
        clock = DEFAULT_CLOCK
        if "clock" in setup:
            value = setup["clock"]
            moment = parse_time(value.get("currentTime")) if isinstance(value, dict) else None
            if moment is None or set(value) != {"currentTime"}:
                raise test_error(400, "invalid clock setup")
            if not self.clock_switch:
                raise test_error(403, "the trusted clock capability is disabled")
            clock = moment
        if session_id in self.sessions:
            raise test_error(409, "session exists")
        for old in setup.get("cleanup_session_ids", []):
            self.cleanup(old)
        self.sessions[session_id] = {
            "faults": dict(faults or {}),
            "clock": clock,
            "admin": subject,
        }
        return httpx.Response(201, json={"session_id": session_id})

    def consume(self, namespace: str | None, key: str) -> bool:
        session = self.sessions.get(namespace or "")
        if session and session["faults"].get(key):
            session["faults"][key] = 0
            return True
        return False

    def cleanup(self, session_id: str) -> None:
        doomed = [w for w, wb in self.workbooks.items() if wb.namespace == session_id]
        super().cleanup(session_id)
        for workbook_id in doomed:
            self.schedules.pop(workbook_id, None)
            self.notifications.pop(workbook_id, None)

    # --- audits, faults and reports -----------------------------------------------------

    def start_audit(
        self,
        version: Version,
        namespace: str | None,
        *,
        fault: bool = True,
        scheduled: bool = False,
    ) -> Audit:
        kind = None
        if scheduled:
            if self.consume(namespace, "scheduled_audit_failures"):
                kind = "scheduled_audit"
            elif self.scheduled_internal_failure:
                kind = "internal"
        elif fault:
            if self.consume(namespace, "audit_failures"):
                kind = "audit"
            elif self.consume(namespace, "report_failures"):
                kind = "report"
        audit = Audit(
            self.new_id("aud"),
            version.version_id,
            "PROCESSING",
            self.now(),
            None,
            [],
            [],
            None,
            self.large_polls if len(version.data) > 1_000_000 else self.processing_polls,
        )
        self.audits[audit.audit_id] = audit
        version.audits.append(audit.audit_id)
        audit.review = [{"fault": kind, "namespace": namespace or self._namespace}]
        if audit.polls_left == 0:
            self.finish(audit)
        return audit

    def finish(self, audit: Audit) -> None:
        marker = audit.review[0] if audit.review else {}
        kind = marker.get("fault")
        previous = self._namespace
        self._namespace = marker.get("namespace", previous)
        try:
            if kind in (None, "audit"):
                audit.review = [{"fault": kind == "audit"}]
                super().finish(audit)
                return
            version = self.versions[audit.version_id]
            audit.completed = self.now()
            audit.status = "PROCESSING_FAILED"
            code, stage, message = {
                "scheduled_audit": (
                    "TEST_SCHEDULED_AUDIT_FAILURE",
                    "scheduled_audit",
                    "Scheduled audit failure requested by the isolated test session.",
                ),
                "report": (
                    "TEST_REPORT_FAILURE",
                    "report",
                    "Report failure requested by the isolated test session.",
                ),
                "internal": ("AUDIT_ENGINE_FAILED", "engine", "The audit engine failed."),
            }[kind]
            details: dict[str, Any] = {"stage": stage}
            if kind != "internal":
                details["testOnly"] = True
            audit.error = {
                "code": code,
                "message": message,
                "requestId": f"job-{audit.audit_id}",
                "details": details,
            }
            audit.review = []
            version.stable_status = "PROCESSING_FAILED"
            report = json.loads(self.build_report(audit, version))
            if kind == "report":
                report["checksPerformed"] = list(ALL_CHECKS)
            audit.report = json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode()
        finally:
            self._namespace = previous

    def build_report(self, audit: Audit, version: Version) -> bytes:
        body = json.loads(super().build_report(audit, version))
        body["specificationVersion"] = "3.0.0"
        return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def upload(
        self, request: httpx.Request, subject: str, namespace: str | None, workbook: Workbook | None
    ) -> httpx.Response:
        before = (set(self.workbooks), set(self.versions), set(self.audits))
        response = super().upload(request, subject, namespace, workbook)
        if response.status_code == 202 and self.consume(namespace, "storage_failures"):
            for table, known in zip(
                (self.workbooks, self.versions, self.audits), before, strict=True
            ):
                for key in set(table) - known:
                    del table[key]  # type: ignore[attr-defined]
            if workbook is not None:
                workbook.versions = [v for v in workbook.versions if v in self.versions]
            raise project_error(500, "TEST_STORAGE_FAILURE")
        return response

    def decisions(
        self,
        request: httpx.Request,
        subject: str,
        namespace: str | None,
        workbook: Workbook,
        version: Version,
    ) -> httpx.Response:
        known = set(self.remediations)
        response = super().decisions(request, subject, namespace, workbook, version)
        for remediation_id in set(self.remediations) - known:
            if self.consume(namespace, "remediation_failures"):
                self.injected_remediations.add(remediation_id)
        return response

    def complete(self, remediation: Remediation, workbook: Workbook) -> None:
        if remediation.remediation_id in self.injected_remediations:
            remediation.status = "PROCESSING_FAILED"
            remediation.error = {
                "code": "TEST_REMEDIATION_FAILURE",
                "message": "Remediation failure requested by the isolated test session.",
                "requestId": f"job-{remediation.remediation_id}",
                "details": {"stage": "remediation", "testOnly": True},
            }
            return
        previous = self._namespace
        self._namespace = workbook.namespace
        try:
            super().complete(remediation, workbook)
        finally:
            self._namespace = previous

    # --- schedules ------------------------------------------------------------------------

    def newest(self, workbook: Workbook) -> Version:
        return self.versions[workbook.versions[-1]]

    def schedule_body(self, workbook: Workbook) -> dict[str, Any]:
        schedule = self.schedules.get(workbook.workbook_id, Schedule())
        return {
            "workbookId": workbook.workbook_id,
            "enabled": schedule.enabled,
            "intervalDays": schedule.interval,
            "complianceDeadline": schedule.deadline,
            "versionId": self.newest(workbook).version_id,
            "nextAuditAt": show(schedule.next_at) if schedule.next_at else None,
            "reminderDays": [90, 30, 7],
        }

    def time_of(self, namespace: str | None) -> datetime:
        return self.effective(namespace) or datetime.now(UTC)

    def configure(
        self, request: httpx.Request, workbook: Workbook, namespace: str | None
    ) -> httpx.Response:
        body = strict_json(request.content)
        if not isinstance(body, dict) or set(body) - {
            "enabled",
            "intervalDays",
            "complianceDeadline",
        }:
            raise project_error(400, "INVALID_REQUEST")
        enabled, interval = body.get("enabled"), body.get("intervalDays")
        deadline = body.get("complianceDeadline")
        if type(enabled) is not bool:
            raise project_error(400, "INVALID_REQUEST")
        valid_interval = type(interval) is int and (
            1 <= interval <= 90 or (self.accept_interval_zero and interval == 0)
        )
        if enabled and not valid_interval:
            raise project_error(400, "INVALID_REQUEST")
        if not enabled and interval is not None and not valid_interval:
            raise project_error(400, "INVALID_REQUEST")
        if deadline is not None and parse_date(deadline) is None:
            raise project_error(400, "INVALID_REQUEST")
        now = self.time_of(namespace)
        old = self.schedules.get(workbook.workbook_id, Schedule())
        new = Schedule(enabled, interval, deadline, old.next_at, old.deadline_configured_at)
        if not enabled:
            new.next_at = None
        elif not old.enabled or old.interval != interval:
            new.next_at = now + timedelta(days=interval)  # type: ignore[arg-type]
        if deadline != old.deadline:
            new.deadline_configured_at = now if deadline else None
        self.schedules[workbook.workbook_id] = new
        return httpx.Response(200, json=self.schedule_body(workbook))

    # --- clock --------------------------------------------------------------------------

    def clock(self, request: httpx.Request) -> httpx.Response:
        namespace = request.headers.get("x-test-session")
        if namespace is None or not self.clock_switch:
            raise project_error(404, "TEST_CLOCK_DISABLED")
        subject = self.subject(request)
        if subject is None:
            raise project_error(401, "UNAUTHORIZED")
        session = self.sessions.get(namespace)
        if session is None:
            raise project_error(400, "INVALID_TEST_SESSION")
        if subject != session["admin"]:
            raise project_error(403, "FORBIDDEN")
        body = strict_json(request.content)
        if not isinstance(body, dict) or set(body) != {"currentTime"}:
            raise project_error(400, "INVALID_REQUEST")
        moment = parse_time(body["currentTime"])
        if moment is None:
            raise project_error(400, "INVALID_REQUEST")
        if moment < session["clock"]:
            raise project_error(400, "CLOCK_REWIND")
        workbooks = sorted(
            (w for w in self.workbooks.values() if w.namespace == namespace),
            key=lambda w: w.workbook_id,
        )
        due = sorted(
            (
                (self.schedules[w.workbook_id].next_at, w.workbook_id, w)
                for w in workbooks
                if w.workbook_id in self.schedules
                and self.schedules[w.workbook_id].enabled
                and self.schedules[w.workbook_id].next_at is not None
                and self.schedules[w.workbook_id].next_at <= moment  # type: ignore[operator]
            ),
            key=lambda item: (item[0], item[1]),
        )
        if not self.clock_ignores_busy and any(self.active(self.newest(w)) for _, _, w in due):
            raise project_error(409, "ACTIVE_OPERATION")
        # Reminder eligibility from the pre-dispatch snapshot.
        reminders = []
        for workbook in workbooks:
            schedule = self.schedules.get(workbook.workbook_id)
            if schedule is None or schedule.deadline is None:
                continue
            day = parse_date(schedule.deadline)
            assert day is not None
            for offset in (90, 30, 7):
                instant = datetime(*(day - timedelta(days=offset)).timetuple()[:3], tzinfo=UTC)
                key = (workbook.workbook_id, schedule.deadline, offset)
                if key in self.considered and not self.no_dedup:
                    continue
                configured = schedule.deadline_configured_at
                if configured is None or not configured <= instant <= moment:
                    continue
                if (
                    self.no_dedup
                    and any(
                        n["scheduledFor"] == show(instant)
                        for n in self.notifications.get(workbook.workbook_id, [])
                    )
                    and moment != instant
                ):
                    continue
                reminders.append(
                    (instant, workbook.workbook_id, -offset, workbook, key, self.eligible(workbook))
                )
        reminders.sort(key=lambda item: item[:3])
        session["clock"] = moment
        self._namespace = namespace
        audits = []
        for next_at, _, workbook in due:
            schedule = self.schedules[workbook.workbook_id]
            audit = self.start_audit(self.newest(workbook), namespace, fault=False, scheduled=True)
            audits.append(audit.audit_id)
            interval = timedelta(days=schedule.interval or 1)
            if self.reset_cadence_to_now:
                schedule.next_at = moment + interval
            else:
                schedule.next_at = next_at + ((moment - next_at) // interval + 1) * interval
        created = []
        for instant, _, _, workbook, key, eligible in reminders:
            self.considered.add(key)
            if not eligible and not self.remind_clean:
                continue
            failed = self.consume(namespace, "notification_failures")
            record = {
                "notificationId": self.new_id("ntf"),
                "type": "COMPLIANCE_DEADLINE_REMINDER",
                "workbookId": workbook.workbook_id,
                "versionId": self.newest(workbook).version_id,
                "scheduledFor": show(instant),
                "status": "FAILED" if failed else "DELIVERED",
                "message": "Reminder recording failed in the isolated test session."
                if failed
                else REMINDER_MESSAGE,
            }
            self.notifications.setdefault(workbook.workbook_id, []).append(record)
            created.append(record["notificationId"])
        return httpx.Response(
            200,
            json={
                "currentTime": show(moment),
                "triggeredAudits": audits,
                "triggeredNotifications": created,
            },
        )

    def eligible(self, workbook: Workbook) -> bool:
        version = self.newest(workbook)
        if not version.audits:
            return True
        audit = self.audits[version.audits[-1]]
        return audit.status in ("PROCESSING", "PROCESSING_FAILED") or bool(audit.findings)

    # --- deletion -----------------------------------------------------------------------

    def delete(self, workbook: Workbook, namespace: str | None) -> httpx.Response:
        if any(self.active(self.versions[v]) for v in workbook.versions):
            raise project_error(409, "ACTIVE_OPERATION")
        if self.consume(namespace, "deletion_failures"):
            raise project_error(500, "TEST_DELETION_FAILURE")
        if self.delete_keeps_schedule:
            # Defect: the schedule row survives deletion and still answers reads.
            self.ghosts[workbook.workbook_id] = self.schedule_body(workbook)
        for version_id in workbook.versions:
            for audit_id in self.versions[version_id].audits:
                self.audits.pop(audit_id, None)
            self.versions.pop(version_id, None)
        for output_id, version_id in list(self.outputs.items()):
            if version_id in workbook.versions:
                del self.outputs[output_id]
        del self.workbooks[workbook.workbook_id]
        self.schedules.pop(workbook.workbook_id, None)
        self.notifications.pop(workbook.workbook_id, None)
        return httpx.Response(204)
