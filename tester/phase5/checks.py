"""Black-box Phase 5 checks (specification 3.0.0). Each returns a pass message or raises a signal.

Oracles stay independent of the system: wire shapes come from the frozen 3.0.0 schema pack,
calendar instants from `tester.phase5.policy`, findings from the hand-written fixture
expectations and the tester's own finding-ID function. Every scenario that moves simulated time
runs in its own fresh namespace (its own durable clock), so one case's clock jumps, schedules
and reminders cannot dispatch another case's work. The main namespace's clock is never
advanced; its effective time is the empty-setup default 2027-01-01T00:00:00Z.

Preparation (namespace setup, uploads, audits, baseline schedules) runs under `setup(ctx)` and
earns no coverage. A refused or failed preparation is still a failed/error observation of this
case, never a pass. Race-dependent observations (an audit or remediation still active when the
clock or DELETE arrives) are asserted only when the active window was actually observed;
otherwise the case is skipped as unobservable and says so.
"""

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

import httpx

from tester.phase4.checks import (
    INVALID_TOKEN,
    SessionChoice,
    body_of,
    bounded,
    call,
    check_audit,
    decision_body,
    download,
    fresh,
    history,
    limit,
    multipart,
    new_key,
    ok,
    plan_of,
    poll_audit,
    poll_remediation,
    project_error,
    report_bytes,
    setup,
    setup_session,
    submit,
    testing_error,
    unknown_session,
    version,
    version_path,
)
from tester.phase5 import policy
from tester.phase5.policy import DAY, DEFAULT_CLOCK, format_timestamp, parse_timestamp
from tester.suite.checks import (
    CheckFailed,
    Prerequisite,
    SuiteContext,
    SystemFault,
    expect_status,
)
from tester.target import Identity, MissingCredential

Json = Any
CLOCK_PATH = "/api/v1/test/clock"
DEADLINE = "2027-04-26"  # frozen reminder vector; thresholds 2027-01-26 / 03-27 / 04-19
LEAP_DEADLINE = "2028-04-26"  # leap-year vector; thresholds 2028-01-27 / 03-27 / 04-19
REMINDER = "COMPLIANCE_DEADLINE_REMINDER"
CONTROLLED_TIMEOUT = re.compile(r"[A-Z0-9_]*TIMEOUT")
SOURCE_LIMIT = "Scheduled audits check the stored version only."
INJECTED = {
    "remediation_failures": (
        "TEST_REMEDIATION_FAILURE",
        "remediation",
        "Remediation failure requested by the isolated test session.",
    ),
    "report_failures": (
        "TEST_REPORT_FAILURE",
        "report",
        "Report failure requested by the isolated test session.",
    ),
    "scheduled_audit_failures": (
        "TEST_SCHEDULED_AUDIT_FAILURE",
        "scheduled_audit",
        "Scheduled audit failure requested by the isolated test session.",
    ),
    "audit_failures": (
        "TEST_AUDIT_FAILURE",
        "audit",
        "Audit failure requested by the isolated test session.",
    ),
}
ALL_CHECKS = [
    "ALT_TEXT",
    "WORKSHEET_NAME",
    "TABLE_HEADER",
    "MERGED_CELLS",
    "LINK_TEXT",
    "COLOR_CONTRAST",
]


# --- namespaces, clock and time ---------------------------------------------------------------


def threshold(deadline: str, offset: int) -> datetime:
    instant = policy.reminder_instants(deadline)[offset]
    assert instant is not None
    return instant


def clocks(ctx: SuiteContext) -> dict[str, datetime]:
    """The tester's own record of each namespace's simulated time (never read from target)."""
    table: dict[str, datetime] = ctx.state.setdefault("p5:clocks", {})
    table.setdefault(ctx.session_id, DEFAULT_CLOCK)
    return table


def now_of(ctx: SuiteContext, session: SessionChoice) -> datetime:
    key = ctx.session_id if session == "main" else str(session)
    return clocks(ctx)[key]


def namespace(ctx: SuiteContext, label: str, setup_body: Json | None = None) -> str:
    """A fresh READY namespace for one scenario; its setup is preparation (no credit)."""
    body = setup_body or {}
    with setup(ctx):
        session_id, response = setup_session(ctx, body)
        expect_status(response, 201, f"{label} namespace setup")
        echoed = body_of(response, "SetupResponse", f"{label} namespace setup")
        if echoed["session_id"] != session_id:
            raise CheckFailed(f"{label} namespace setup did not echo its session_id.")
    clock = (body.get("clock") or {}).get("currentTime")
    clocks(ctx)[session_id] = parse_timestamp(clock) if clock else DEFAULT_CLOCK
    return session_id


def clock_request(
    ctx: SuiteContext,
    session: str | None,
    current_time: object,
    *,
    identity: Identity | None = "test-admin",
    raw_token: str | None = None,
    raw: bytes | None = None,
) -> httpx.Response:
    headers = {"X-Test-Session": session} if session else {}
    kwargs: dict[str, Any] = (
        {"content": raw, "headers": {**headers, "Content-Type": "application/json"}}
        if raw is not None
        else {"json": {"currentTime": current_time}, "headers": headers}
    )
    try:
        return ctx.client.request(
            "POST",
            CLOCK_PATH,
            identity=identity,
            raw_token=raw_token,
            session_header=False,
            timeout=limit(ctx, "business_get_seconds", 10),
            max_bytes=limit(ctx, "json_response_bytes", 67_108_864),
            **kwargs,
        )
    except MissingCredential as exc:
        raise Prerequisite(f"Not executed: {exc}.") from None


def advance(
    ctx: SuiteContext,
    session: str,
    moment: datetime,
    action: str,
    *,
    expected_audits: int | None = None,
    expected_notifications: int | None = None,
) -> Json:
    """Move a namespace clock and check the ClockResponse envelope and fresh IDs."""
    response = clock_request(ctx, session, format_timestamp(moment))
    body = ok(ctx, response, 200, "ClockResponse", action)
    if parse_timestamp(body["currentTime"]) != moment:
        raise CheckFailed(
            f"{action} reported currentTime {bounded(body['currentTime'], 40)}; "
            f"expected {format_timestamp(moment)}."
        )
    clocks(ctx)[session] = moment
    fresh(ctx, list(body["triggeredAudits"]), action)
    # Notification IDs are their own identifier space: the contract does not require them to
    # differ from workbook/version/audit IDs, only never to be listed twice.
    seen: set[str] = ctx.state.setdefault("p5:notification_ids", set())
    if seen & set(body["triggeredNotifications"]):
        raise CheckFailed(f"{action} listed an already recorded notification ID again.")
    seen.update(body["triggeredNotifications"])
    for name, expected, key in (
        ("audits", expected_audits, "triggeredAudits"),
        ("notifications", expected_notifications, "triggeredNotifications"),
    ):
        if expected is not None and len(body[key]) != expected:
            raise CheckFailed(
                f"{action} accepted {len(body[key])} scheduled {name}; expected {expected}."
            )
    return body


def unchanged_clock(ctx: SuiteContext, session: str, action: str) -> None:
    """An equal-time call is allowed; a 400 CLOCK_REWIND would prove the time moved forward."""
    moment = clocks(ctx)[session]
    response = clock_request(ctx, session, format_timestamp(moment))
    if response.status_code == 400:
        raise CheckFailed(f"{action}: the namespace clock moved past {format_timestamp(moment)}.")
    body = ok(ctx, response, 200, "ClockResponse", f"{action} equal-time probe")
    if parse_timestamp(body["currentTime"]) != moment:
        raise CheckFailed(f"{action}: the namespace clock is no longer {format_timestamp(moment)}.")
    if body["triggeredAudits"] or body["triggeredNotifications"]:
        raise CheckFailed(f"{action}: an equal-time probe found newly due work.")


# --- uploads, schedules and notifications ------------------------------------------------------


def upload_as(
    ctx: SuiteContext,
    fixture: str,
    action: str,
    *,
    session: SessionChoice,
    identity: Identity = "userA",
    workbook_id: str | None = None,
) -> dict[str, str]:
    accepted = ctx.suite.accepted(fixture)
    path = "/api/v1/workbooks" + (f"/{workbook_id}/versions" if workbook_id else "")
    response = call(
        ctx,
        "POST",
        path,
        identity=identity,
        session=session,
        files=multipart(ctx.suite.bytes_of(fixture), accepted.upload_name),
    )
    ids: dict[str, str] = ok(ctx, response, 202, "UploadResponse", action)
    if workbook_id is not None and ids["workbookId"] != workbook_id:
        raise CheckFailed(f"{action} returned a different workbookId.")
    fresh(
        ctx,
        [ids["versionId"], ids["auditId"]] + ([] if workbook_id else [ids["workbookId"]]),
        action,
    )
    return ids


def audited_in(
    ctx: SuiteContext,
    fixture: str,
    session: SessionChoice,
    label: str,
    *,
    identity: Identity = "userA",
    workbook_id: str | None = None,
) -> tuple[dict[str, str], Json]:
    """Preparation: upload and wait for the fixture's independently expected audit."""
    with setup(ctx):
        ids = upload_as(
            ctx,
            fixture,
            f"{label} upload",
            session=session,
            identity=identity,
            workbook_id=workbook_id,
        )
        audit = poll_audit(ctx, ids, f"{label} audit", session=session, identity=identity)
        check_audit(ctx, audit, fixture, f"{label} audit")
    return ids, audit


def schedule_path(ids: Mapping[str, str]) -> str:
    return f"/api/v1/workbooks/{ids['workbookId']}/schedule"


def put_schedule(
    ctx: SuiteContext,
    session: SessionChoice,
    ids: Mapping[str, str],
    body: Json,
    *,
    identity: Identity | None = "userA",
    raw_token: str | None = None,
    raw: bytes | None = None,
) -> httpx.Response:
    kwargs: dict[str, Any] = (
        {"content": raw, "headers": {"Content-Type": "application/json"}}
        if raw is not None
        else {"json": body}
    )
    return call(
        ctx,
        "PUT",
        schedule_path(ids),
        identity=identity,
        raw_token=raw_token,
        session=session,
        timeout=limit(ctx, "business_get_seconds", 10),
        **kwargs,
    )


def check_schedule(
    body: Json,
    ids: Mapping[str, str],
    action: str,
    *,
    enabled: bool,
    interval: int | None,
    deadline: str | None,
    next_at: datetime | None,
    version_id: str | None = None,
) -> None:
    if body["workbookId"] != ids["workbookId"]:
        raise CheckFailed(f"{action} names a different workbook.")
    observed = (body["enabled"], body["intervalDays"], body["complianceDeadline"])
    if observed != (enabled, interval, deadline):
        raise CheckFailed(
            f"{action} returned enabled/interval/deadline {observed}; "
            f"expected {(enabled, interval, deadline)}."
        )
    actual_next = parse_timestamp(body["nextAuditAt"]) if body["nextAuditAt"] else None
    if actual_next != next_at:
        raise CheckFailed(
            f"{action} nextAuditAt is {body['nextAuditAt']}; expected "
            f"{format_timestamp(next_at) if next_at else None}."
        )
    wanted = version_id or ids["versionId"]
    if body["versionId"] != wanted:
        raise CheckFailed(f"{action} selected version {body['versionId']}; expected {wanted}.")


def configure(
    ctx: SuiteContext,
    session: SessionChoice,
    ids: Mapping[str, str],
    body: Json,
    action: str,
    *,
    next_at: datetime | None,
    version_id: str | None = None,
) -> Json:
    response = put_schedule(ctx, session, ids, body)
    result = ok(ctx, response, 200, "ScheduleResponse", action)
    check_schedule(
        result,
        ids,
        action,
        enabled=body["enabled"],
        interval=body.get("intervalDays") if body.get("intervalDays") is not None else None,
        deadline=body.get("complianceDeadline"),
        next_at=next_at,
        version_id=version_id,
    )
    return result


def read_schedule(
    ctx: SuiteContext,
    session: SessionChoice,
    ids: Mapping[str, str],
    action: str,
    *,
    identity: Identity = "userA",
) -> Json:
    response = call(ctx, "GET", schedule_path(ids), identity=identity, session=session)
    return ok(ctx, response, 200, "ScheduleResponse", action)


def notifications(
    ctx: SuiteContext,
    session: SessionChoice,
    ids: Mapping[str, str],
    action: str,
    *,
    identity: Identity = "userA",
) -> list[Json]:
    response = call(
        ctx,
        "GET",
        f"/api/v1/workbooks/{ids['workbookId']}/notifications",
        identity=identity,
        session=session,
    )
    records: list[Json] = ok(ctx, response, 200, "NotificationResponse", action)["notifications"]
    previous = None
    for record in records:
        if record["workbookId"] != ids["workbookId"]:
            raise CheckFailed(f"{action} lists a notification for another workbook.")
        if record["type"] != REMINDER:
            raise CheckFailed(
                f"{action} lists a {record['type']} record; only {REMINDER} is emitted."
            )
        moment = parse_timestamp(record["scheduledFor"])
        if previous is not None and moment < previous:
            raise CheckFailed(f"{action} is not ordered by scheduledFor, oldest first.")
        previous = moment
    ids_seen = [record["notificationId"] for record in records]
    if len(ids_seen) != len(set(ids_seen)):
        raise CheckFailed(f"{action} repeats a notificationId.")
    return records


def expect_records(
    records: list[Json], expected: list[tuple[str, datetime, str]], action: str
) -> None:
    observed = [(r["versionId"], parse_timestamp(r["scheduledFor"]), r["status"]) for r in records]
    if observed != expected:

        def show(rows: list[tuple[str, datetime, str]]) -> str:
            return "; ".join(f"{v} {format_timestamp(t)} {s}" for v, t, s in rows) or "none"

        raise CheckFailed(
            f"{action}: notifications were [{show(observed)}]; expected [{show(expected)}]."
        )


def audits_of(
    ctx: SuiteContext,
    session: SessionChoice,
    ids: Mapping[str, str],
    action: str,
    *,
    identity: Identity = "userA",
) -> list[Json]:
    audits: list[Json] = history(ctx, ids, action, session=session, identity=identity)["audits"]
    return audits


def poll_scheduled(
    ctx: SuiteContext,
    session: SessionChoice,
    ids: Mapping[str, str],
    audit_id: str,
    version_id: str,
    action: str,
    *,
    identity: Identity = "userA",
    allow_failure: bool = False,
) -> Json:
    return poll_audit(
        ctx,
        {**ids, "versionId": version_id},
        action,
        audit_id=audit_id,
        version_id=version_id,
        session=session,
        identity=identity,
        allow_failure=allow_failure,
    )


def scheduled_audit_checks(
    ctx: SuiteContext,
    session: str,
    ids: Mapping[str, str],
    clock: Json,
    fixture: str,
    action: str,
    *,
    version_id: str | None = None,
    prior_audits: int = 1,
) -> Json:
    """Exactly one fresh audit on the selected stored version; bytes and versions unchanged."""
    selected = version_id or ids["versionId"]
    (audit_id,) = clock["triggeredAudits"]
    audit = poll_scheduled(ctx, session, ids, audit_id, selected, f"{action} scheduled audit")
    check_audit(ctx, audit, fixture, f"{action} scheduled audit")
    record = history(ctx, ids, f"{action} history", session=session)
    if [a["auditId"] for a in record["audits"]][-1] != audit_id:
        raise CheckFailed(f"{action}: history does not end with the scheduled audit.")
    if len(record["audits"]) != prior_audits + 1:
        raise CheckFailed(
            f"{action}: history lists {len(record['audits'])} audits; expected {prior_audits + 1}."
        )
    current = version(ctx, ids, f"{action} version", version_id=selected, session=session)
    if current["latestAuditId"] != audit_id:
        raise CheckFailed(f"{action}: latestAuditId does not identify the scheduled audit.")
    source, _ = download(
        ctx, f"{version_path(ids, selected)}/source", f"{action} source", session=session
    )
    if hashlib.sha256(source).hexdigest() != current["sha256"]:
        raise CheckFailed(f"{action}: the stored source bytes changed.")
    return audit


# --- schedules (AC-12) --------------------------------------------------------------------------


def _main_base(ctx: SuiteContext, key: str, fixture: str = "clean_all_six") -> dict[str, str]:
    cache = f"p5:main:{key}"
    if cache not in ctx.state:
        ids, _ = audited_in(ctx, fixture, "main", f"Main {key}")
        ctx.state[cache] = ids
    return dict(ctx.state[cache])


def schedule_default(ctx: SuiteContext) -> str:
    ids = _main_base(ctx, "default")
    first = read_schedule(ctx, "main", ids, "Unconfigured schedule read")
    check_schedule(
        first,
        ids,
        "Unconfigured schedule read",
        enabled=False,
        interval=None,
        deadline=None,
        next_at=None,
    )
    second = read_schedule(ctx, "main", ids, "Repeated unconfigured schedule read")
    if second != first:
        raise CheckFailed("A repeated unconfigured schedule read differs; GET must create nothing.")
    if len(audits_of(ctx, "main", ids, "History after schedule reads")) != 1:
        raise CheckFailed("Reading the default schedule created audit work.")
    return (
        "GET returned the disabled/null default policy for the latest version and created nothing."
    )


def schedule_bound(ctx: SuiteContext) -> str:
    interval = int(str(ctx.parameters["interval"]))
    ids = _main_base(ctx, f"bound-{interval}")
    now = now_of(ctx, "main")
    body = {"enabled": True, "intervalDays": interval, "complianceDeadline": None}
    configure(
        ctx,
        "main",
        ids,
        body,
        f"Enable interval {interval}",
        next_at=policy.initial_due(now, interval),
    )
    message = (
        f"Interval {interval} was accepted with nextAuditAt exactly {interval} UTC day(s) later."
    )
    leap = ctx.parameters.get("leap_clock")
    if isinstance(leap, str):
        session = namespace(ctx, "Leap-year interval", {"clock": {"currentTime": leap}})
        leap_ids, _ = audited_in(ctx, "clean_all_six", session, "Leap-year interval")
        start = parse_timestamp(leap)
        configure(
            ctx,
            session,
            leap_ids,
            body,
            f"Enable interval {interval} from {leap}",
            next_at=policy.initial_due(start, interval),
        )
        message += f" From {leap} it is {format_timestamp(policy.initial_due(start, interval))}."
    return message


def _schedule_scenario(
    ctx: SuiteContext, label: str, *, fixture: str = "clean_all_six", setup_body: Json | None = None
) -> tuple[str, dict[str, str]]:
    session = namespace(ctx, label, setup_body)
    ids, _ = audited_in(ctx, fixture, session, label)
    return session, ids


def schedule_replay(ctx: SuiteContext) -> str:
    session, ids = _schedule_scenario(ctx, "Schedule replay")
    start = now_of(ctx, session)
    body = {"enabled": True, "intervalDays": 10, "complianceDeadline": "2027-12-31"}
    with setup(ctx):
        configure(ctx, session, ids, body, "Initial schedule", next_at=start + 10 * DAY)
        advance(ctx, session, start + 2 * DAY, "Advance two days", expected_audits=0)
    replay = configure(
        ctx, session, ids, body, "Identical schedule PUT replay", next_at=start + 10 * DAY
    )
    if read_schedule(ctx, session, ids, "Schedule after replay") != replay:
        raise CheckFailed("The persisted schedule differs from the replayed PUT response.")
    if len(audits_of(ctx, session, ids, "History after replay")) != 1:
        raise CheckFailed("An identical schedule PUT created audit work.")
    unchanged_clock(ctx, session, "After replay")
    return "An identical normalized PUT preserved nextAuditAt and created no events."


def schedule_deadline_only(ctx: SuiteContext) -> str:
    session, ids = _schedule_scenario(ctx, "Deadline-only change")
    start = now_of(ctx, session)
    with setup(ctx):
        configure(
            ctx,
            session,
            ids,
            {"enabled": True, "intervalDays": 10, "complianceDeadline": None},
            "Initial schedule",
            next_at=start + 10 * DAY,
        )
        advance(ctx, session, start + 3 * DAY, "Advance three days", expected_audits=0)
    configure(
        ctx,
        session,
        ids,
        {"enabled": True, "intervalDays": 10, "complianceDeadline": "2027-12-31"},
        "Deadline-only change",
        next_at=start + 10 * DAY,
    )
    read = read_schedule(ctx, session, ids, "Schedule after deadline-only change")
    check_schedule(
        read,
        ids,
        "Schedule after deadline-only change",
        enabled=True,
        interval=10,
        deadline="2027-12-31",
        next_at=start + 10 * DAY,
    )
    return "Changing only the deadline preserved the periodic due time."


def schedule_interval_change(ctx: SuiteContext) -> str:
    session, ids = _schedule_scenario(ctx, "Interval change")
    start = now_of(ctx, session)
    with setup(ctx):
        configure(
            ctx,
            session,
            ids,
            {"enabled": True, "intervalDays": 10},
            "Initial schedule",
            next_at=start + 10 * DAY,
        )
        advance(ctx, session, start + 3 * DAY, "Advance three days", expected_audits=0)
    configure(
        ctx,
        session,
        ids,
        {"enabled": True, "intervalDays": 5},
        "Interval change",
        next_at=start + 3 * DAY + 5 * DAY,
    )
    return "Changing the interval restarted cadence from the current effective time."


def schedule_disable(ctx: SuiteContext) -> str:
    session, ids = _schedule_scenario(ctx, "Disable schedule")
    start = now_of(ctx, session)
    with setup(ctx):
        configure(
            ctx,
            session,
            ids,
            {"enabled": True, "intervalDays": 1},
            "Initial schedule",
            next_at=start + DAY,
        )
    before = audits_of(ctx, session, ids, "History before disabling")
    configure(ctx, session, ids, {"enabled": False}, "Disable periodic audits", next_at=None)
    advance(
        ctx,
        session,
        start + 5 * DAY,
        "Advance past every former due time",
        expected_audits=0,
        expected_notifications=0,
    )
    after = audits_of(ctx, session, ids, "History after disabling")
    if after != before:
        raise CheckFailed("Disabling changed the audit history or created a later audit.")
    return "Disabled periodic auditing created no later audit and the older history remained."


def schedule_reenable(ctx: SuiteContext) -> str:
    session, ids = _schedule_scenario(ctx, "Re-enable schedule")
    start = now_of(ctx, session)
    with setup(ctx):
        configure(
            ctx,
            session,
            ids,
            {"enabled": True, "intervalDays": 1},
            "Initial schedule",
            next_at=start + DAY,
        )
        configure(ctx, session, ids, {"enabled": False}, "Disable", next_at=None)
        advance(ctx, session, start + DAY * 5 / 2, "Advance while disabled", expected_audits=0)
    again = start + DAY * 5 / 2
    configure(
        ctx, session, ids, {"enabled": True, "intervalDays": 1}, "Re-enable", next_at=again + DAY
    )
    clock = advance(
        ctx, session, again + DAY, "Advance to the re-enabled due time", expected_audits=1
    )
    scheduled_audit_checks(ctx, session, ids, clock, "clean_all_six", "Re-enabled")
    return (
        "Re-enabling started cadence from the re-enable time; the next due time dispatched one "
        "audit."
    )


def schedule_before_due(ctx: SuiteContext) -> str:
    session, ids = _schedule_scenario(ctx, "Before due")
    start = now_of(ctx, session)
    with setup(ctx):
        configure(
            ctx,
            session,
            ids,
            {"enabled": True, "intervalDays": 1},
            "Initial schedule",
            next_at=start + DAY,
        )
    almost = start + DAY - timedelta(microseconds=1)
    advance(
        ctx,
        session,
        almost,
        "Advance to one microsecond before due",
        expected_audits=0,
        expected_notifications=0,
    )
    read = read_schedule(ctx, session, ids, "Schedule before due")
    check_schedule(
        read,
        ids,
        "Schedule before due",
        enabled=True,
        interval=1,
        deadline=None,
        next_at=start + DAY,
    )
    if len(audits_of(ctx, session, ids, "History before due")) != 1:
        raise CheckFailed("An audit was created before the due instant.")
    return "One microsecond before nextAuditAt no scheduled audit was accepted."


def schedule_exact_due(ctx: SuiteContext) -> str:
    session, ids = _schedule_scenario(ctx, "Exact due")
    start = now_of(ctx, session)
    with setup(ctx):
        configure(
            ctx,
            session,
            ids,
            {"enabled": True, "intervalDays": 1},
            "Initial schedule",
            next_at=start + DAY,
        )
    clock = advance(ctx, session, start + DAY, "Advance exactly to due", expected_audits=1)
    audit = scheduled_audit_checks(ctx, session, ids, clock, "clean_all_six", "Exact-due")
    if audit["rulesetVersion"] != ctx.suite.manifest.ruleset_version:
        raise CheckFailed("The scheduled audit used a different ruleset.")
    record = history(ctx, ids, "Exact-due versions", session=session)
    if [v["versionId"] for v in record["versions"]] != [ids["versionId"]]:
        raise CheckFailed("The scheduled audit created a workbook version.")
    read = read_schedule(ctx, session, ids, "Schedule after dispatch")
    check_schedule(
        read,
        ids,
        "Schedule after dispatch",
        enabled=True,
        interval=1,
        deadline=None,
        next_at=start + 2 * DAY,
    )
    return "At the due instant exactly one fresh audit ran on the stored version; bytes unchanged."


def schedule_coalesce(ctx: SuiteContext) -> str:
    session, ids = _schedule_scenario(ctx, "Coalesce")
    start = now_of(ctx, session)
    due = start + DAY
    with setup(ctx):
        configure(
            ctx, session, ids, {"enabled": True, "intervalDays": 1}, "Initial schedule", next_at=due
        )
    jump = start + DAY * 9 / 2
    audits, expected_next = policy.dispatch(due, 1, jump)
    clock = advance(ctx, session, jump, "Jump several intervals", expected_audits=audits)
    read = read_schedule(ctx, session, ids, "Schedule after coalescing")
    check_schedule(
        read,
        ids,
        "Schedule after coalescing",
        enabled=True,
        interval=1,
        deadline=None,
        next_at=expected_next,
    )
    scheduled_audit_checks(ctx, session, ids, clock, "clean_all_six", "Coalesced")
    return (
        "Missed periods coalesced into one audit; next due stayed on the grid at "
        f"{format_timestamp(expected_next)}."
    )


def schedule_same_time(ctx: SuiteContext) -> str:
    session, ids = _schedule_scenario(ctx, "Same-time replay")
    start = now_of(ctx, session)
    with setup(ctx):
        configure(
            ctx,
            session,
            ids,
            {"enabled": True, "intervalDays": 1},
            "Initial schedule",
            next_at=start + DAY,
        )
        first = advance(ctx, session, start + DAY, "First dispatch", expected_audits=1)
        poll_scheduled(
            ctx, session, ids, first["triggeredAudits"][0], ids["versionId"], "First dispatch audit"
        )
    replay = advance(
        ctx,
        session,
        start + DAY,
        "Replay the same timestamp",
        expected_audits=0,
        expected_notifications=0,
    )
    if set(replay["triggeredAudits"]) & set(first["triggeredAudits"]):
        raise CheckFailed("The replay returned an already accepted audit ID.")
    if len(audits_of(ctx, session, ids, "History after replay")) != 2:
        raise CheckFailed("The same-timestamp replay created a duplicate audit.")
    return "Replaying the committed timestamp returned no IDs and created no duplicate audit."


def schedule_latest_upload(ctx: SuiteContext) -> str:
    session, ids = _schedule_scenario(ctx, "Latest upload")
    changed = str(ctx.parameters["changed"])
    start = now_of(ctx, session)
    with setup(ctx):
        configure(
            ctx,
            session,
            ids,
            {"enabled": True, "intervalDays": 2},
            "Initial schedule",
            next_at=start + 2 * DAY,
        )
        advance(ctx, session, start + DAY, "Advance one day", expected_audits=0)
    new, _ = audited_in(ctx, changed, session, "New version", workbook_id=ids["workbookId"])
    read = read_schedule(ctx, session, ids, "Schedule after upload")
    check_schedule(
        read,
        ids,
        "Schedule after upload",
        enabled=True,
        interval=2,
        deadline=None,
        next_at=start + 2 * DAY,
        version_id=new["versionId"],
    )
    clock = advance(
        ctx, session, start + 2 * DAY, "Advance to the unchanged due time", expected_audits=1
    )
    scheduled_audit_checks(
        ctx,
        session,
        ids,
        clock,
        changed,
        "Latest-upload",
        version_id=new["versionId"],
        prior_audits=2,
    )
    return (
        "The new upload became the selected version without postponing nextAuditAt; dispatch "
        "audited it."
    )


def _remediate(
    ctx: SuiteContext,
    session: SessionChoice,
    ids: Mapping[str, str],
    audit: Json,
    fixture: str,
    plan: str,
    action: str,
    *,
    wait: bool = True,
) -> tuple[Json, Json | None]:
    details = plan_of(ctx, fixture, plan)
    body = decision_body(ctx, fixture, audit["auditId"], details["decisions"])
    accepted = ok(
        ctx,
        submit(ctx, ids, body, key=new_key(), session=session),
        202,
        "RemediationAccepted",
        f"{action} decisions",
    )
    fresh(ctx, [accepted["remediationId"]], f"{action} decisions")
    if not wait:
        return accepted, None
    status = poll_remediation(
        ctx, accepted["remediationId"], f"{action} remediation", session=session
    )
    return accepted, status


def schedule_latest_output(ctx: SuiteContext) -> str:
    fixture, plan = str(ctx.parameters["fixture"]), str(ctx.parameters["plan"])
    session, ids = _schedule_scenario(ctx, "Latest output", fixture=fixture)
    start = now_of(ctx, session)
    with setup(ctx):
        latest = version(ctx, ids, "Source version", session=session)
        audit = poll_audit(
            ctx, ids, "Source audit", audit_id=latest["latestAuditId"], session=session
        )
        configure(
            ctx,
            session,
            ids,
            {"enabled": True, "intervalDays": 2},
            "Initial schedule",
            next_at=start + 2 * DAY,
        )
        advance(ctx, session, start + DAY, "Advance one day", expected_audits=0)
        _, status = _remediate(ctx, session, ids, audit, fixture, plan, "Latest output")
        assert status is not None
        if status["status"] != "REMEDIATED":
            raise CheckFailed(f"The preparatory remediation ended {status['status']}.")
        poll_scheduled(
            ctx, session, ids, status["auditId"], status["outputVersionId"], "Output audit"
        )
    output_version = status["outputVersionId"]
    read = read_schedule(ctx, session, ids, "Schedule after publication")
    check_schedule(
        read,
        ids,
        "Schedule after publication",
        enabled=True,
        interval=2,
        deadline=None,
        next_at=start + 2 * DAY,
        version_id=output_version,
    )
    clock = advance(
        ctx, session, start + 2 * DAY, "Advance to the unchanged due time", expected_audits=1
    )
    (audit_id,) = clock["triggeredAudits"]
    scheduled = poll_scheduled(
        ctx, session, ids, audit_id, output_version, "Output scheduled audit"
    )
    if scheduled["status"] == "PROCESSING_FAILED":
        raise SystemFault("The scheduled audit of the generated version ended PROCESSING_FAILED.")
    published = version(ctx, ids, "Output version", version_id=output_version, session=session)
    if published["sourceVersionId"] != ids["versionId"] or published["latestAuditId"] != audit_id:
        raise CheckFailed("The generated version lost its provenance or latest-audit pointer.")
    return (
        "The generated output became the selected version; dispatch audited it and kept provenance."
    )


def schedule_old_reaudit(ctx: SuiteContext) -> str:
    session, ids = _schedule_scenario(ctx, "Old re-audit")
    start = now_of(ctx, session)
    new, _ = audited_in(
        ctx, "clean_all_six", session, "Second version", workbook_id=ids["workbookId"]
    )
    with setup(ctx):
        configure(
            ctx,
            session,
            ids,
            {"enabled": True, "intervalDays": 1},
            "Initial schedule",
            next_at=start + DAY,
            version_id=new["versionId"],
        )
        response = call(ctx, "POST", f"{version_path(ids)}/audits", session=session, json={})
        accepted = ok(ctx, response, 202, "ReauditResponse", "Re-audit of the older version")
        poll_audit(ctx, ids, "Older re-audit", audit_id=accepted["auditId"], session=session)
    read = read_schedule(ctx, session, ids, "Schedule after old re-audit")
    check_schedule(
        read,
        ids,
        "Schedule after old re-audit",
        enabled=True,
        interval=1,
        deadline=None,
        next_at=start + DAY,
        version_id=new["versionId"],
    )
    clock = advance(ctx, session, start + DAY, "Advance to due", expected_audits=1)
    scheduled_audit_checks(
        ctx,
        session,
        new,
        clock,
        "clean_all_six",
        "Old-reaudit",
        version_id=new["versionId"],
        prior_audits=3,
    )
    return "Re-auditing the older version did not change the newest-version selection."


def schedule_busy(ctx: SuiteContext) -> str:
    kind = str(ctx.parameters["kind"])
    fixture = str(ctx.parameters["fixture"])
    session, ids = _schedule_scenario(ctx, f"Busy {kind}", fixture=fixture)
    start = now_of(ctx, session)
    with setup(ctx):
        latest = version(ctx, ids, "Source version", session=session)
        audit = poll_audit(
            ctx, ids, "Source audit", audit_id=latest["latestAuditId"], session=session
        )
        configure(
            ctx,
            session,
            ids,
            {"enabled": True, "intervalDays": 1},
            "Initial schedule",
            next_at=start + DAY,
        )
    with setup(ctx):
        if kind == "audit":
            response = call(ctx, "POST", f"{version_path(ids)}/audits", session=session, json={})
            accepted = ok(ctx, response, 202, "ReauditResponse", "Manual re-audit")
            active_id = accepted["auditId"]
        else:
            accepted, _ = _remediate(
                ctx, session, ids, audit, fixture, str(ctx.parameters["plan"]), "Busy", wait=False
            )
            active_id = accepted["remediationId"]
    clock = clock_request(ctx, session, format_timestamp(start + DAY))
    if clock.status_code == 200:
        # Was the work still active when the clock call arrived? Only an observation proves it.
        if kind == "audit":
            still = call(ctx, "GET", f"{version_path(ids)}/audits/{active_id}", session=session)
            active = (
                still.status_code == 200
                and body_of(still, "AuditResponse", "Re-audit")["status"] == "PROCESSING"
            )
        else:
            still = call(ctx, "GET", f"/api/v1/remediations/{active_id}", session=session)
            active = (
                still.status_code == 200
                and body_of(still, "RemediationResponse", "Remediation")["status"] == "REMEDIATING"
            )
        clocks(ctx)[session] = start + DAY
        if active:
            ctx.qualify()
            raise CheckFailed(
                f"The clock dispatched while the selected version's {kind} was still active."
            )
        raise Prerequisite(
            f"Not executed: the {kind} finished before the clock call, so the busy window was "
            "not observable (the clock call was accepted normally)."
        )
    project_error(ctx, clock, 409, f"Clock advance during an active {kind}", "ACTIVE_OPERATION")
    unchanged_clock(ctx, session, f"After the refused advance during an active {kind}")
    read = read_schedule(ctx, session, ids, "Schedule after the refused advance")
    check_schedule(
        read,
        ids,
        "Schedule after the refused advance",
        enabled=True,
        interval=1,
        deadline=None,
        next_at=start + DAY,
        version_id=read["versionId"],
    )
    with setup(ctx):
        if kind == "audit":
            poll_audit(ctx, ids, "Manual re-audit", audit_id=active_id, session=session)
        else:
            status = poll_remediation(ctx, active_id, "Busy remediation", session=session)
            if status["status"] == "REMEDIATED" and status["auditId"]:
                poll_scheduled(
                    ctx, session, ids, status["auditId"], status["outputVersionId"], "Output audit"
                )
    retried = advance(
        ctx, session, start + DAY, "Identical clock request after completion", expected_audits=1
    )
    selected = read_schedule(ctx, session, ids, "Schedule after the retried advance")["versionId"]
    poll_scheduled(
        ctx, session, ids, retried["triggeredAudits"][0], selected, "Retried scheduled audit"
    )
    return (
        f"An active {kind} made the clock return 409 with no time/cadence change; the identical "
        "retry then dispatched."
    )


def schedule_two_workbooks(ctx: SuiteContext) -> str:
    session = namespace(ctx, "Two workbooks")
    start = now_of(ctx, session)
    first, _ = audited_in(ctx, "clean_all_six", session, "UserA workbook")
    second, _ = audited_in(ctx, "clean_all_six", session, "UserB workbook", identity="userB")
    with setup(ctx):
        configure(
            ctx,
            session,
            first,
            {"enabled": True, "intervalDays": 1},
            "UserA schedule",
            next_at=start + DAY,
        )
        response = put_schedule(
            ctx, session, second, {"enabled": True, "intervalDays": 1}, identity="userB"
        )
        expect_status(response, 200, "UserB schedule")
    clock = advance(ctx, session, start + DAY, "One advance for both", expected_audits=2)
    owners: dict[str, tuple[Identity, dict[str, str]]] = {
        first["workbookId"]: ("userA", first),
        second["workbookId"]: ("userB", second),
    }
    found: dict[str, str] = {}
    for workbook_id, (identity, ids) in owners.items():
        for audit in audits_of(ctx, session, ids, f"{identity} history", identity=identity):
            if audit["auditId"] in clock["triggeredAudits"]:
                found[audit["auditId"]] = workbook_id
        foreign: Identity = "userB" if identity == "userA" else "userA"
        denied = call(
            ctx,
            "GET",
            f"/api/v1/workbooks/{workbook_id}/history",
            identity=foreign,
            session=session,
        )
        project_error(ctx, denied, 403, f"{foreign} reading {identity}'s history", "FORBIDDEN")
    if set(found) != set(clock["triggeredAudits"]) or sorted(set(found.values())) != sorted(owners):
        raise CheckFailed("Each workbook did not receive exactly one of the triggered audits.")
    order = [found[a] for a in clock["triggeredAudits"]]
    if order != sorted(order):
        raise CheckFailed("Same-due scheduled audits are not ordered by workbookId.")
    for audit_id, workbook_id in found.items():
        identity, ids = owners[workbook_id]
        poll_scheduled(
            ctx,
            session,
            ids,
            audit_id,
            ids["versionId"],
            f"{identity} scheduled audit",
            identity=identity,
        )
    return (
        "One advance dispatched both owners' workbooks in workbookId order without cross-owner "
        "access."
    )


def schedule_failed_next(ctx: SuiteContext) -> str:
    session, ids = _schedule_scenario(
        ctx, "Failed scheduled audit", setup_body={"faults": {"scheduled_audit_failures": 1}}
    )
    message = _scheduled_failure_then_recovery(ctx, session, ids, "Failed-next")
    return message


def _scheduled_failure_then_recovery(
    ctx: SuiteContext, session: str, ids: Mapping[str, str], label: str
) -> str:
    start = now_of(ctx, session)
    with setup(ctx):
        configure(
            ctx,
            session,
            ids,
            {"enabled": True, "intervalDays": 1},
            "Initial schedule",
            next_at=start + DAY,
        )
    clock = advance(ctx, session, start + DAY, "First scheduled occurrence", expected_audits=1)
    (failed_id,) = clock["triggeredAudits"]
    failed = poll_scheduled(
        ctx,
        session,
        ids,
        failed_id,
        ids["versionId"],
        "Injected scheduled audit",
        allow_failure=True,
    )
    code, stage, text = INJECTED["scheduled_audit_failures"]
    _expect_injected(failed, code, stage, text, f"{label} scheduled audit")
    raw, report = report_bytes(ctx, ids, failed_id, f"{label} failed report", session=session)
    if (report["status"], report["checksPerformed"], report["findings"]) != (
        "PROCESSING_FAILED",
        [],
        [],
    ):
        raise CheckFailed(
            f"{label}: the failed report shows checks or findings for a pre-rule failure."
        )
    source, _ = download(ctx, f"{version_path(ids)}/source", f"{label} source", session=session)
    if source != ctx.suite.bytes_of("clean_all_six"):
        raise CheckFailed(f"{label}: the source bytes changed after the failed scheduled audit.")
    read = read_schedule(ctx, session, ids, f"{label} schedule after the failure")
    check_schedule(
        read,
        ids,
        f"{label} schedule after the failure",
        enabled=True,
        interval=1,
        deadline=None,
        next_at=start + 2 * DAY,
    )
    retry = advance(
        ctx, session, start + DAY, "Same timestamp after the failure", expected_audits=0
    )
    del retry
    nxt = advance(ctx, session, start + 2 * DAY, "Next interval", expected_audits=1)
    recovered = scheduled_audit_checks(
        ctx, session, ids, nxt, "clean_all_six", f"{label} next", prior_audits=2
    )
    del recovered
    old_raw, _ = report_bytes(
        ctx, ids, failed_id, f"{label} retained failed report", session=session
    )
    if old_raw != raw:
        raise CheckFailed(f"{label}: the failed audit's report bytes changed after recovery.")
    return (
        f"Namespace {session}: the injected scheduled failure kept the source and cadence, was not "
        "retried automatically, and the next interval audited normally."
    )


def _expect_injected(audit: Json, code: str, stage: str, message: str, action: str) -> None:
    if audit["status"] != "PROCESSING_FAILED":
        raise CheckFailed(f"{action} ended {audit['status']}, not PROCESSING_FAILED.")
    error = audit["error"]
    if (error["code"], error["message"], error["details"]) != (
        code,
        message,
        {"stage": stage, "testOnly": True},
    ):
        raise CheckFailed(f"{action} does not carry the exact {code} error envelope.")
    if audit["findings"] or audit["completedAt"] is None:
        raise CheckFailed(f"{action} exposes partial findings or lacks completedAt.")
    if parse_timestamp(audit["completedAt"]) < parse_timestamp(audit["startedAt"]):
        raise CheckFailed(f"{action} completed before it started.")


SCHEDULE_BASELINE = {"enabled": True, "intervalDays": 7, "complianceDeadline": "2027-06-30"}


def schedule_reject(ctx: SuiteContext) -> str:
    ids = _main_base(ctx, "reject")
    with setup(ctx):
        response = put_schedule(ctx, "main", ids, SCHEDULE_BASELINE)
        expect_status(response, 200, "Baseline schedule")
        baseline = body_of(response, "ScheduleResponse", "Baseline schedule")
    # Each invalid input replaces one field of an otherwise valid body, so only it is tested.
    body = {**SCHEDULE_BASELINE, **dict(ctx.parameters["body"])}  # type: ignore[arg-type]
    if body.get("intervalDays") == "<omit>":
        del body["intervalDays"]
    response = put_schedule(ctx, "main", ids, body)
    project_error(
        ctx, response, 400, f"Invalid schedule {bounded(json.dumps(body), 90)}", "INVALID_REQUEST"
    )
    after = read_schedule(ctx, "main", ids, "Schedule after the rejected PUT")
    if after != baseline:
        raise CheckFailed("The rejected PUT changed the previous configuration.")
    return (
        "The invalid schedule returned 400 INVALID_REQUEST and the prior configuration was "
        "unchanged."
    )


# --- reminders (AC-13) ---------------------------------------------------------------------------


def _reminder_base(
    ctx: SuiteContext,
    label: str,
    fixture: str,
    *,
    setup_body: Json | None = None,
    deadline: str = DEADLINE,
    enabled: bool = False,
) -> tuple[str, dict[str, str], datetime]:
    session, ids = _schedule_scenario(ctx, label, fixture=fixture, setup_body=setup_body)
    configured = now_of(ctx, session)
    body: Json = {"enabled": enabled, "complianceDeadline": deadline}
    next_at = None
    if enabled:
        body["intervalDays"] = 90
        next_at = configured + 90 * DAY
    with setup(ctx):
        configure(ctx, session, ids, body, f"{label} deadline", next_at=next_at)
    return session, ids, configured


def _single_reminder(
    ctx: SuiteContext, offset: int, *, clock_start: str | None = None, enabled: bool = False
) -> str:
    setup_body = {"clock": {"currentTime": clock_start}} if clock_start else None
    session, ids, _ = _reminder_base(
        ctx, f"{offset}-day reminder", "seeded_all_six", setup_body=setup_body, enabled=enabled
    )
    instant = threshold(DEADLINE, offset)
    clock = advance(
        ctx,
        session,
        instant,
        f"Advance to the {offset}-day threshold",
        expected_audits=0,
        expected_notifications=1,
    )
    records = notifications(ctx, session, ids, f"{offset}-day notifications")
    expect_records(records, [(ids["versionId"], instant, "DELIVERED")], f"{offset}-day reminder")
    if [r["notificationId"] for r in records] != clock["triggeredNotifications"]:
        raise CheckFailed("The clock response does not list exactly the new notification record.")
    return f"One DELIVERED reminder was recorded at exactly {format_timestamp(instant)}."


def reminder(ctx: SuiteContext) -> str:
    return REMINDER_CASES[str(ctx.parameters["case"])](ctx)


def _r90(ctx: SuiteContext) -> str:
    return _single_reminder(ctx, 90, enabled=True)


def _r30(ctx: SuiteContext) -> str:
    return _single_reminder(ctx, 30, clock_start="2027-03-26T00:00:00Z")


def _r7(ctx: SuiteContext) -> str:
    return _single_reminder(ctx, 7, clock_start="2027-04-18T00:00:00Z")


def _r_before(ctx: SuiteContext) -> str:
    session, ids, _ = _reminder_base(ctx, "Before threshold", "seeded_all_six")
    almost = threshold(DEADLINE, 90) - timedelta(microseconds=1)
    advance(
        ctx,
        session,
        almost,
        "Advance to just before the 90-day threshold",
        expected_notifications=0,
    )
    expect_records(notifications(ctx, session, ids, "Notifications before"), [], "Before threshold")
    return "No notification exists one microsecond before the 90-day threshold."


def _r_clean(ctx: SuiteContext) -> str:
    session, ids, _ = _reminder_base(ctx, "Clean suppression", "clean_all_six")
    advance(
        ctx, session, threshold(DEADLINE, 90), "Advance to the threshold", expected_notifications=0
    )
    expect_records(notifications(ctx, session, ids, "Clean notifications"), [], "Clean suppression")
    return "A latest successful clean audit suppressed the 90-day reminder."


def _observed_active_reminder(ctx: SuiteContext, label: str, *, partial: bool) -> str:
    """Eligibility of a latest audit that is still PROCESSING when the threshold is processed."""
    session = namespace(ctx, label)
    with setup(ctx):
        ids = upload_as(ctx, "large_preservation", f"{label} upload", session=session)
        response = put_schedule(
            ctx, session, ids, {"enabled": False, "complianceDeadline": DEADLINE}
        )
        expect_status(response, 200, f"{label} deadline")

    def processing() -> Json | None:
        response = call(ctx, "GET", f"{version_path(ids)}/audits/{ids['auditId']}", session=session)
        expect_status(response, 200, f"{label} audit read")
        body = body_of(response, "AuditResponse", f"{label} audit read")
        return body if body["status"] == "PROCESSING" else None

    before = processing()
    instant = threshold(DEADLINE, 90)
    clock = advance(ctx, session, instant, f"{label} threshold")
    after = processing()
    with setup(ctx):
        poll_audit(ctx, ids, f"{label} audit", session=session)
    if before is None or after is None:
        raise Prerequisite(
            "Not executed: the audit was not observed PROCESSING on both sides of the clock "
            "call, so the active window was not observable."
        )
    if partial and (after["findings"] or after["completedAt"] is not None):
        raise CheckFailed(f"{label}: the PROCESSING audit exposes partial findings or completion.")
    records = notifications(ctx, session, ids, f"{label} notifications")
    expect_records(records, [(ids["versionId"], instant, "DELIVERED")], label)
    if len(clock["triggeredNotifications"]) != 1:
        raise CheckFailed(f"{label}: the clock response does not list the reminder.")
    return f"{label}: an audit still PROCESSING at the threshold made the version eligible."


def _r_no_audit(ctx: SuiteContext) -> str:
    return _observed_active_reminder(ctx, "Without completed audit", partial=False)


def _r_processing(ctx: SuiteContext) -> str:
    return _observed_active_reminder(ctx, "Processing audit", partial=True)


def _r_failed(ctx: SuiteContext) -> str:
    session = namespace(ctx, "Failed audit eligibility", {"faults": {"audit_failures": 1}})
    with setup(ctx):
        ids = upload_as(ctx, "clean_all_six", "Failed-audit upload", session=session)
        failed = poll_audit(ctx, ids, "Injected audit", session=session, allow_failure=True)
        if failed["status"] != "PROCESSING_FAILED":
            raise CheckFailed("The armed audit fault did not produce PROCESSING_FAILED.")
        configure(
            ctx,
            session,
            ids,
            {"enabled": False, "complianceDeadline": DEADLINE},
            "Deadline",
            next_at=None,
        )
    instant = threshold(DEADLINE, 90)
    advance(ctx, session, instant, "Advance to the threshold", expected_notifications=1)
    expect_records(
        notifications(ctx, session, ids, "Failed-audit notifications"),
        [(ids["versionId"], instant, "DELIVERED")],
        "Failed audit eligibility",
    )
    return "A latest PROCESSING_FAILED audit (clean bytes) remained eligible for the reminder."


def _r_declined(ctx: SuiteContext) -> str:
    fixture = "review_only"
    session, ids, _ = _reminder_base(ctx, "Declined findings", fixture)
    with setup(ctx):
        latest = version(ctx, ids, "Version", session=session)
        audit = poll_audit(ctx, ids, "Audit", audit_id=latest["latestAuditId"], session=session)
        _, status = _remediate(ctx, session, ids, audit, fixture, "no_edits", "Decline all")
        assert status is not None
        if status["status"] != "REMEDIATED":
            raise CheckFailed(f"The decline/manual-review batch ended {status['status']}.")
        selected = read_schedule(ctx, session, ids, "Schedule after decisions")["versionId"]
        if status["auditId"]:
            poll_scheduled(ctx, session, ids, status["auditId"], selected, "Post-decision audit")
    instant = threshold(DEADLINE, 90)
    advance(ctx, session, instant, "Advance to the threshold", expected_notifications=1)
    expect_records(
        notifications(ctx, session, ids, "Declined notifications"),
        [(selected, instant, "DELIVERED")],
        "Declined findings",
    )
    return "DECLINE and MANUAL_REVIEW decisions left findings unresolved; the reminder was emitted."


def _r_periodic_off(ctx: SuiteContext) -> str:
    session, ids, _ = _reminder_base(ctx, "Periodic disabled", "seeded_all_six", enabled=False)
    instant = threshold(DEADLINE, 90)
    advance(
        ctx,
        session,
        instant,
        "Advance to the threshold",
        expected_audits=0,
        expected_notifications=1,
    )
    expect_records(
        notifications(ctx, session, ids, "Notifications"),
        [(ids["versionId"], instant, "DELIVERED")],
        "Periodic disabled",
    )
    return (
        "With periodic auditing disabled, the deadline reminder still ran and no audit was created."
    )


def _r_clear(ctx: SuiteContext) -> str:
    session, ids, _ = _reminder_base(ctx, "Clear deadline", "seeded_all_six")
    first = threshold(DEADLINE, 90)
    with setup(ctx):
        advance(ctx, session, first, "Advance to the 90-day threshold", expected_notifications=1)
    before = notifications(ctx, session, ids, "Notifications before clearing")
    configure(
        ctx,
        session,
        ids,
        {"enabled": False, "complianceDeadline": None},
        "Clear the deadline",
        next_at=None,
    )
    advance(
        ctx,
        session,
        threshold(DEADLINE, 7) + DAY,
        "Advance past the remaining thresholds",
        expected_notifications=0,
    )
    if notifications(ctx, session, ids, "Notifications after clearing") != before:
        raise CheckFailed(
            "Clearing the deadline changed or removed the earlier record, or added one."
        )
    return "A null deadline stopped future reminders and left the earlier record unchanged."


def _r_no_backfill(ctx: SuiteContext) -> str:
    session, ids, _ = _reminder_base(
        ctx,
        "No backfill",
        "seeded_all_six",
        setup_body={"clock": {"currentTime": "2027-02-01T00:00:00Z"}},
    )
    instant = threshold(DEADLINE, 30)
    advance(ctx, session, instant, "Advance to the 30-day threshold", expected_notifications=1)
    expect_records(
        notifications(ctx, session, ids, "Notifications"),
        [(ids["versionId"], instant, "DELIVERED")],
        "No backfill",
    )
    return "A deadline configured after the 90-day threshold produced only the 30-day reminder."


def _r_config_exact(ctx: SuiteContext) -> str:
    instant = threshold(DEADLINE, 90)
    session, ids, _ = _reminder_base(
        ctx,
        "Configured at threshold",
        "seeded_all_six",
        setup_body={"clock": {"currentTime": format_timestamp(instant)}},
    )
    advance(ctx, session, instant, "Equal-time processing pass", expected_notifications=1)
    advance(ctx, session, instant, "Second equal-time pass", expected_notifications=0)
    expect_records(
        notifications(ctx, session, ids, "Notifications"),
        [(ids["versionId"], instant, "DELIVERED")],
        "Configured at threshold",
    )
    return "Configuring exactly at a threshold allowed one equal-time reminder and no repeat."


def _r_jump(ctx: SuiteContext) -> str:
    session, ids, configured = _reminder_base(ctx, "Forward jump", "seeded_all_six")
    jump = threshold(DEADLINE, 7) + DAY
    offsets = policy.crossed_offsets(configured, jump, DEADLINE)
    clock = advance(
        ctx, session, jump, "Jump across all thresholds", expected_notifications=len(offsets)
    )
    records = notifications(ctx, session, ids, "Notifications")
    expect_records(
        records,
        [(ids["versionId"], threshold(DEADLINE, o), "DELIVERED") for o in offsets],
        "Forward jump",
    )
    if [r["notificationId"] for r in records] != clock["triggeredNotifications"]:
        raise CheckFailed(
            "Triggered notification IDs are not in scheduledFor (offset 90/30/7) order."
        )
    return "One jump considered all three crossed thresholds in 90/30/7 order."


def _r_dedup(ctx: SuiteContext) -> str:
    session, ids, _ = _reminder_base(ctx, "Deduplication", "seeded_all_six")
    instant = threshold(DEADLINE, 90)
    with setup(ctx):
        advance(ctx, session, instant, "Advance to the threshold", expected_notifications=1)
    first = notifications(ctx, session, ids, "Notifications")
    advance(ctx, session, instant, "Repeat the timestamp", expected_notifications=0)
    response = put_schedule(ctx, session, ids, {"enabled": False, "complianceDeadline": DEADLINE})
    ok(ctx, response, 200, "ScheduleResponse", "Repeat the identical PUT")
    advance(ctx, session, instant + DAY, "Advance one more day", expected_notifications=0)
    if notifications(ctx, session, ids, "Notifications after repeats") != first:
        raise CheckFailed("Repeated clock/PUT calls duplicated or changed the occurrence.")
    return (
        "Repeated clock timestamps, an identical PUT and a later tick did not duplicate the "
        "reminder."
    )


def _r_clean_then_dirty(ctx: SuiteContext) -> str:
    session, ids, _ = _reminder_base(ctx, "Clean then dirty", "clean_all_six")
    first = threshold(DEADLINE, 90)
    advance(ctx, session, first, "Clean threshold pass", expected_notifications=0)
    new, _ = audited_in(
        ctx, "seeded_all_six", session, "Unresolved new version", workbook_id=ids["workbookId"]
    )
    advance(ctx, session, first + DAY, "Advance after the new version", expected_notifications=0)
    second = threshold(DEADLINE, 30)
    advance(ctx, session, second, "Advance to the 30-day threshold", expected_notifications=1)
    expect_records(
        notifications(ctx, session, ids, "Notifications"),
        [(new["versionId"], second, "DELIVERED")],
        "Clean then dirty",
    )
    return (
        "A clean, considered 90-day occurrence was never emitted later; the 30-day one "
        "followed the new version."
    )


def _r_return(ctx: SuiteContext) -> str:
    session, ids, _ = _reminder_base(ctx, "Return to deadline", "seeded_all_six")
    other = "2027-05-26"
    first = threshold(DEADLINE, 90)
    with setup(ctx):
        advance(ctx, session, first, "Advance to the first threshold", expected_notifications=1)
    configure(
        ctx,
        session,
        ids,
        {"enabled": False, "complianceDeadline": other},
        "Change deadline",
        next_at=None,
    )
    configure(
        ctx,
        session,
        ids,
        {"enabled": False, "complianceDeadline": DEADLINE},
        "Return to the first deadline",
        next_at=None,
    )
    advance(ctx, session, first + DAY, "Advance one day", expected_notifications=0)
    second = threshold(DEADLINE, 30)
    advance(ctx, session, second, "Advance to the 30-day threshold", expected_notifications=1)
    expect_records(
        notifications(ctx, session, ids, "Notifications"),
        [(ids["versionId"], first, "DELIVERED"), (ids["versionId"], second, "DELIVERED")],
        "Return to deadline",
    )
    return "Returning to an earlier deadline never reissued its considered 90-day key."


def _r_edit(ctx: SuiteContext) -> str:
    session, ids, _ = _reminder_base(ctx, "Deadline edit", "seeded_all_six")
    first = threshold(DEADLINE, 90)
    with setup(ctx):
        advance(ctx, session, first, "Advance to the first threshold", expected_notifications=1)
    before = notifications(ctx, session, ids, "Notifications before the edit")
    moved = "2027-06-01"
    configure(
        ctx,
        session,
        ids,
        {"enabled": False, "complianceDeadline": moved},
        "Move the deadline",
        next_at=None,
    )
    new_instant = threshold(moved, 90)
    advance(
        ctx, session, new_instant, "Advance to the new 90-day threshold", expected_notifications=1
    )
    advance(
        ctx,
        session,
        threshold(DEADLINE, 30) + DAY,
        "Advance past the old 30-day threshold",
        expected_notifications=0,
    )
    records = notifications(ctx, session, ids, "Notifications after the edit")
    if records[: len(before)] != before:
        raise CheckFailed("Editing the deadline changed the earlier notification record.")
    expect_records(
        records,
        [(ids["versionId"], first, "DELIVERED"), (ids["versionId"], new_instant, "DELIVERED")],
        "Deadline edit",
    )
    return "The new deadline produced its own future key; the earlier record stayed unchanged."


def _r_latest_version(ctx: SuiteContext) -> str:
    session, ids, _ = _reminder_base(ctx, "Latest version", "clean_all_six")
    new, _ = audited_in(
        ctx, "seeded_all_six", session, "Unresolved new version", workbook_id=ids["workbookId"]
    )
    instant = threshold(DEADLINE, 90)
    advance(ctx, session, instant, "Advance to the threshold", expected_notifications=1)
    expect_records(
        notifications(ctx, session, ids, "Notifications"),
        [(new["versionId"], instant, "DELIVERED")],
        "Latest version",
    )
    return "The reminder named the newest unresolved version, not the earlier clean one."


def _r_same_tick(ctx: SuiteContext) -> str:
    session, ids = _schedule_scenario(ctx, "Same-tick audit")
    start = now_of(ctx, session)
    instant = threshold(DEADLINE, 90)
    interval = (instant - start) // DAY
    with setup(ctx):
        configure(
            ctx,
            session,
            ids,
            {"enabled": True, "intervalDays": interval, "complianceDeadline": DEADLINE},
            "Schedule due at the threshold",
            next_at=start + interval * DAY,
        )
    clock = advance(
        ctx,
        session,
        instant,
        "Advance to the shared instant",
        expected_audits=1,
        expected_notifications=0,
    )
    expect_records(notifications(ctx, session, ids, "Notifications"), [], "Same-tick audit")
    poll_scheduled(
        ctx,
        session,
        ids,
        clock["triggeredAudits"][0],
        ids["versionId"],
        "Same-tick scheduled audit",
    )
    return (
        "The pre-dispatch clean snapshot suppressed the reminder although the tick enqueued an "
        "audit."
    )


def _r_leap(ctx: SuiteContext) -> str:
    session, ids, configured = _reminder_base(
        ctx,
        "Leap-year thresholds",
        "seeded_all_six",
        setup_body={"clock": {"currentTime": "2028-01-01T00:00:00Z"}},
        deadline=LEAP_DEADLINE,
    )
    jump = threshold(LEAP_DEADLINE, 7) + DAY
    offsets = policy.crossed_offsets(configured, jump, LEAP_DEADLINE)
    advance(ctx, session, jump, "Jump across the leap-year thresholds", expected_notifications=3)
    expect_records(
        notifications(ctx, session, ids, "Notifications"),
        [(ids["versionId"], threshold(LEAP_DEADLINE, o), "DELIVERED") for o in offsets],
        "Leap-year thresholds",
    )
    return "Leap-year thresholds matched the frozen UTC calendar vector (2028-01-27, 03-27, 04-19)."


def _r_failed_record(ctx: SuiteContext) -> str:
    return _notification_fault(ctx, "Notification failure (AC-13)")


def _notification_fault(ctx: SuiteContext, label: str) -> str:
    session, ids, _ = _reminder_base(
        ctx, label, "seeded_all_six", setup_body={"faults": {"notification_failures": 1}}
    )
    audit_before = version(ctx, ids, f"{label} version before", session=session)
    first = threshold(DEADLINE, 90)
    clock = advance(ctx, session, first, f"{label} first threshold", expected_notifications=1)
    records = notifications(ctx, session, ids, f"{label} notifications")
    expect_records(records, [(ids["versionId"], first, "FAILED")], label)
    if records[0]["notificationId"] != clock["triggeredNotifications"][0]:
        raise CheckFailed(f"{label}: the clock response does not list the FAILED record.")
    if version(ctx, ids, f"{label} version after", session=session) != audit_before:
        raise CheckFailed(f"{label}: the notification failure changed version/audit state.")
    advance(ctx, session, first, f"{label} same timestamp", expected_notifications=0)
    second = threshold(DEADLINE, 30)
    advance(ctx, session, second, f"{label} next threshold", expected_notifications=1)
    expect_records(
        notifications(ctx, session, ids, f"{label} later notifications"),
        [(ids["versionId"], first, "FAILED"), (ids["versionId"], second, "DELIVERED")],
        label,
    )
    return (
        f"Namespace {session}: the injected failure recorded FAILED once, was not retried and "
        "left audits unchanged."
    )


def _r_empty(ctx: SuiteContext) -> str:
    ids = _main_base(ctx, "empty-notifications")
    response = call(ctx, "GET", f"/api/v1/workbooks/{ids['workbookId']}/notifications")
    body = ok(ctx, response, 200, "NotificationResponse", "Notifications without reminders")
    if body != {"notifications": []}:
        raise CheckFailed("A workbook without reminders did not return exactly an empty array.")
    return 'A workbook without reminders returned exactly {"notifications": []}.'


def _r_order(ctx: SuiteContext) -> str:
    session, ids, _ = _reminder_base(ctx, "Order", "seeded_all_six")
    with setup(ctx):
        advance(
            ctx,
            session,
            threshold(DEADLINE, 7) + DAY,
            "Jump across all thresholds",
            expected_notifications=3,
        )
    first = notifications(ctx, session, ids, "First read")
    second = notifications(ctx, session, ids, "Second read")
    if first != second or len(first) != 3:
        raise CheckFailed("Repeated notification reads differ or do not list three records.")
    return "Three records are ordered by scheduledFor and identical across repeated reads."


REMINDER_CASES: dict[str, Callable[[SuiteContext], str]] = {
    "90": _r90,
    "30": _r30,
    "7": _r7,
    "before": _r_before,
    "clean": _r_clean,
    "no_audit": _r_no_audit,
    "processing": _r_processing,
    "failed_audit": _r_failed,
    "declined": _r_declined,
    "periodic_off": _r_periodic_off,
    "clear_deadline": _r_clear,
    "no_backfill": _r_no_backfill,
    "config_exact": _r_config_exact,
    "jump": _r_jump,
    "dedup": _r_dedup,
    "clean_then_dirty": _r_clean_then_dirty,
    "return_deadline": _r_return,
    "deadline_edit": _r_edit,
    "latest_version": _r_latest_version,
    "audit_same_tick": _r_same_tick,
    "leap_utc": _r_leap,
    "failed_record": _r_failed_record,
    "empty": _r_empty,
    "order": _r_order,
}


# --- isolated clock (AC-12, C6/C10) ---------------------------------------------------------------


def clock(ctx: SuiteContext) -> str:
    return CLOCK_CASES[str(ctx.parameters["case"])](ctx)


def _c_default(ctx: SuiteContext) -> str:
    session = namespace(ctx, "Default clock")
    advance(
        ctx,
        session,
        DEFAULT_CLOCK,
        "Equal-time call at the default",
        expected_audits=0,
        expected_notifications=0,
    )
    earlier = DEFAULT_CLOCK - timedelta(microseconds=1)
    project_error(
        ctx,
        clock_request(ctx, session, format_timestamp(earlier)),
        400,
        "One microsecond before the default",
        "CLOCK_REWIND",
    )
    return "A fresh empty setup's clock is exactly 2027-01-01T00:00:00Z."


def _c_custom(ctx: SuiteContext) -> str:
    custom = "2027-03-15T12:30:00.5Z"
    session = namespace(ctx, "Custom clock", {"clock": {"currentTime": custom}})
    moment = parse_timestamp(custom)
    advance(ctx, session, moment, "Equal-time call at the custom time")
    project_error(
        ctx,
        clock_request(ctx, session, format_timestamp(moment - timedelta(microseconds=1))),
        400,
        "One microsecond before the custom time",
        "CLOCK_REWIND",
    )
    return f"setup.clock.currentTime {custom} initialized the namespace clock exactly."


def _c_rewind(ctx: SuiteContext) -> str:
    session, ids = _schedule_scenario(ctx, "Rewind")
    start = now_of(ctx, session)
    with setup(ctx):
        configure(
            ctx,
            session,
            ids,
            {"enabled": True, "intervalDays": 2},
            "Schedule",
            next_at=start + 2 * DAY,
        )
        advance(ctx, session, start + DAY, "Advance one day")
    project_error(
        ctx,
        clock_request(ctx, session, format_timestamp(start + DAY / 2)),
        400,
        "Backward timestamp",
        "CLOCK_REWIND",
    )
    unchanged_clock(ctx, session, "After the rejected rewind")
    check_schedule(
        read_schedule(ctx, session, ids, "Schedule after rewind"),
        ids,
        "Schedule after rewind",
        enabled=True,
        interval=2,
        deadline=None,
        next_at=start + 2 * DAY,
    )
    return "A backward timestamp returned 400 CLOCK_REWIND with no time, cadence or event change."


def _c_same(ctx: SuiteContext) -> str:
    instant = threshold(DEADLINE, 90)
    session = namespace(
        ctx, "Same timestamp", {"clock": {"currentTime": format_timestamp(instant)}}
    )
    advance(
        ctx,
        session,
        instant,
        "Equal-time call with nothing due",
        expected_audits=0,
        expected_notifications=0,
    )
    ids, _ = audited_in(ctx, "seeded_all_six", session, "Same timestamp")
    with setup(ctx):
        configure(
            ctx,
            session,
            ids,
            {"enabled": False, "complianceDeadline": DEADLINE},
            "Deadline at now",
            next_at=None,
        )
    advance(ctx, session, instant, "Equal-time call after new due work", expected_notifications=1)
    advance(
        ctx,
        session,
        instant,
        "Repeated equal-time call",
        expected_audits=0,
        expected_notifications=0,
    )
    return (
        "An identical timestamp is allowed and processes only newly configured work due at "
        "that instant."
    )


def _c_utc(ctx: SuiteContext) -> str:
    session = namespace(ctx, "Timestamp format")
    invalid: list[tuple[str, bytes]] = [
        ("offset", b'{"currentTime":"2027-01-02T00:00:00+00:00"}'),
        ("negative offset", b'{"currentTime":"2027-01-02T00:00:00-05:00"}'),
        ("space separator", b'{"currentTime":"2027-01-02 00:00:00Z"}'),
        ("seven fraction digits", b'{"currentTime":"2027-01-02T00:00:00.1234567Z"}'),
        ("invalid month", b'{"currentTime":"2027-13-02T00:00:00Z"}'),
        ("date only", b'{"currentTime":"2027-01-02"}'),
        ("number", b'{"currentTime":1798761600}'),
        ("missing", b"{}"),
        ("extra field", b'{"currentTime":"2027-01-02T00:00:00Z","extra":1}'),
        (
            "duplicate key",
            b'{"currentTime":"2027-01-02T00:00:00Z","currentTime":"2027-01-03T00:00:00Z"}',
        ),
    ]
    for label, raw in invalid:
        project_error(
            ctx,
            clock_request(ctx, session, None, raw=raw),
            400,
            f"Clock body ({label})",
            "INVALID_REQUEST",
        )
    unchanged_clock(ctx, session, "After rejected timestamps")
    return (
        f"{len(invalid)} non-Z, malformed or non-exact clock bodies returned 400 without moving "
        "time."
    )


def _c_isolation(ctx: SuiteContext) -> str:
    first = namespace(ctx, "Isolation A")
    second = namespace(ctx, "Isolation B")
    start = DEFAULT_CLOCK
    a_ids, _ = audited_in(ctx, "clean_all_six", first, "Isolation A")
    b_ids, _ = audited_in(ctx, "clean_all_six", second, "Isolation B")
    with setup(ctx):
        configure(
            ctx,
            first,
            a_ids,
            {"enabled": True, "intervalDays": 1},
            "A schedule",
            next_at=start + DAY,
        )
        configure(
            ctx,
            second,
            b_ids,
            {"enabled": True, "intervalDays": 1},
            "B schedule",
            next_at=start + DAY,
        )
        ordinary = upload_as(ctx, "clean_all_six", "Ordinary workbook", session="none")
        poll_audit(ctx, ordinary, "Ordinary audit", session="none")
        response = put_schedule(ctx, "none", ordinary, {"enabled": True, "intervalDays": 1})
        expect_status(response, 200, "Ordinary schedule")
        ordinary_before = body_of(response, "ScheduleResponse", "Ordinary schedule")
    try:
        advance(ctx, first, start + 9 * DAY, "Advance namespace A nine days", expected_audits=1)
        unchanged_clock(ctx, second, "Namespace B")
        check_schedule(
            read_schedule(ctx, second, b_ids, "B schedule"),
            b_ids,
            "B schedule",
            enabled=True,
            interval=1,
            deadline=None,
            next_at=start + DAY,
        )
        if len(audits_of(ctx, second, b_ids, "B history")) != 1:
            raise CheckFailed("Advancing namespace A dispatched work in namespace B.")
        if read_schedule(ctx, "none", ordinary, "Ordinary schedule after") != ordinary_before:
            raise CheckFailed("Advancing namespace A changed ordinary schedule state.")
        if len(audits_of(ctx, "none", ordinary, "Ordinary history")) != 1:
            raise CheckFailed("Advancing namespace A dispatched ordinary work.")
    finally:
        with setup(ctx):
            # Ordinary data is the user's real data space: remove the probe through owner deletion.
            call(
                ctx,
                "DELETE",
                f"/api/v1/workbooks/{ordinary['workbookId']}",
                session="none",
                timeout=limit(ctx, "delete_seconds", 30),
            )
    return (
        "Advancing namespace A left namespace B's clock/schedule and ordinary real-time state "
        "unchanged."
    )


def _c_owner(ctx: SuiteContext) -> str:
    session = namespace(ctx, "Clock owner")
    later = DEFAULT_CLOCK + timedelta(hours=1)
    callers: tuple[Identity, ...] = ("userA", "reviewer", "test-adminB")
    for identity in callers:
        if identity == "test-adminB" and not ctx.client.has_identity("test-adminB"):
            continue
        response = clock_request(ctx, session, format_timestamp(later), identity=identity)
        project_error(ctx, response, 403, f"Clock advance by {identity}", "FORBIDDEN")
        unchanged_clock(ctx, session, f"After the refused {identity} advance")
    advance(ctx, session, later, "Clock advance by the creating test-admin")
    tested = "user, reviewer" + (
        ", foreign test-admin" if ctx.client.has_identity("test-adminB") else ""
    )
    return f"Only the creating test-admin advanced the namespace ({tested} refused 403)."


def _c_disabled(ctx: SuiteContext) -> str:
    response = clock_request(ctx, None, format_timestamp(DEFAULT_CLOCK + DAY))
    project_error(ctx, response, 404, "Clock call without a test namespace", "TEST_CLOCK_DISABLED")
    return "A clock call outside a designated namespace returned 404 TEST_CLOCK_DISABLED."


def _c_cleaned(ctx: SuiteContext) -> str:
    unknown = f"t9-unknown-{new_key('ns')}"[:64]
    response = clock_request(ctx, unknown, format_timestamp(DEFAULT_CLOCK + DAY))
    project_error(
        ctx, response, 400, "Clock call naming an unknown session", "INVALID_TEST_SESSION"
    )
    scratch = namespace(ctx, "Cleaned clock")
    with setup(ctx):
        carrier, response = setup_session(ctx, {"cleanup_session_ids": [scratch]})
        expect_status(response, 201, "Cleanup carrier setup")
    response = clock_request(ctx, scratch, format_timestamp(DEFAULT_CLOCK + DAY))
    project_error(ctx, response, 400, "Clock call naming a cleaned session", "INVALID_TEST_SESSION")
    return (
        f"Unknown and cleaned namespaces ({scratch}, cleaned by {carrier}) returned 400 "
        "INVALID_TEST_SESSION."
    )


def _c_not_ready(ctx: SuiteContext) -> str:
    raise Prerequisite(
        "Not executed: the HTTP interface offers no way to hold a known namespace in a "
        "preparing/failed state; SESSION_NOT_READY is left to target local integration."
    )


CLOCK_CASES: dict[str, Callable[[SuiteContext], str]] = {
    "default": _c_default,
    "custom": _c_custom,
    "rewind": _c_rewind,
    "same": _c_same,
    "utc": _c_utc,
    "isolation": _c_isolation,
    "owner": _c_owner,
    "disabled": _c_disabled,
    "cleaned": _c_cleaned,
    "not_ready": _c_not_ready,
}


def clock_access(ctx: SuiteContext) -> str:
    identity = str(ctx.parameters["identity"])
    status = int(str(ctx.parameters["expected_status"]))
    session = namespace(ctx, f"Clock access {identity}")
    later = DEFAULT_CLOCK + timedelta(hours=1)
    who: dict[str, tuple[Identity | None, str | None]] = {
        "no_token": (None, None),
        "invalid_token": (None, INVALID_TOKEN),
        "owner_user": ("userA", None),
        "reviewer": ("reviewer", None),
        "foreign_admin": ("test-adminB", None),
        "creating_admin": ("test-admin", None),
    }
    caller, token = who[identity]
    if caller == "test-adminB" and not ctx.client.has_identity("test-adminB"):
        raise Prerequisite(
            "Not executed: no second test-admin credential (test-adminB) is provisioned for "
            "this target, so the foreign-admin boundary cannot be exercised."
        )
    response = clock_request(
        ctx, session, format_timestamp(later), identity=caller, raw_token=token
    )
    if status == 200:
        advance_body = ok(ctx, response, 200, "ClockResponse", "Creating test-admin advance")
        if parse_timestamp(advance_body["currentTime"]) != later:
            raise CheckFailed("The creating test-admin's advance reported a different time.")
        clocks(ctx)[session] = later
        unchanged_clock(ctx, session, "After the creating test-admin advance")
        return "The creating test-admin advanced its own READY namespace (200)."
    code = {401: "UNAUTHORIZED", 403: "FORBIDDEN"}[status]
    project_error(ctx, response, status, f"Clock advance as {identity}", code)
    unchanged_clock(ctx, session, f"After the refused {identity} advance")
    return (
        f"Clock advance as {identity} returned {status} {code}; the namespace time was unchanged."
    )


# --- authorization matrix for new operations (AC-15) -------------------------------------------


def _access_resource(ctx: SuiteContext, owner: Identity, *, per_case: bool) -> dict[str, str]:
    if per_case:
        ids, _ = audited_in(ctx, "clean_all_six", "main", f"Access {owner}", identity=owner)
        return ids
    cache = f"p5:access:{owner}"
    if cache not in ctx.state:
        ids, _ = audited_in(ctx, "clean_all_six", "main", f"Access {owner}", identity=owner)
        ctx.state[cache] = ids
    return dict(ctx.state[cache])


def access_p5(ctx: SuiteContext) -> str:
    operation = str(ctx.parameters["operation"])
    variant = str(ctx.parameters["identity"])
    status = int(str(ctx.parameters["expected_status"]))
    owner: Identity = "userB" if variant == "ungranted_reviewer" else "userA"
    ids = _access_resource(ctx, owner, per_case=operation == "delete")
    identity: Identity | None = owner
    token: str | None = None
    session: SessionChoice = "main"
    if variant == "no_token":
        identity = None
    elif variant == "invalid_token":
        identity, token = None, INVALID_TOKEN
    elif variant == "foreign_owner":
        identity = "userB"
    elif variant == "wrong_namespace":
        session = "none"
    elif variant == "test_admin":
        identity = "test-admin"
    elif variant in ("granted_reviewer", "ungranted_reviewer"):
        identity = "reviewer"
    target = {"workbookId": "t9-unknown-workbook"} if variant == "unknown_resource" else ids
    with setup(ctx):
        before = read_schedule(ctx, "main", ids, "Owner schedule before", identity=owner)
        notes_before = notifications(ctx, "main", ids, "Owner notifications before", identity=owner)
    action = f"{operation} as {variant}"
    if operation == "schedule_write":
        body = {"enabled": True, "intervalDays": 30, "complianceDeadline": "2027-09-30"}
        response = put_schedule(ctx, session, target, body, identity=identity, raw_token=token)
    elif operation == "delete":
        response = call(
            ctx,
            "DELETE",
            f"/api/v1/workbooks/{target['workbookId']}",
            identity=identity,
            raw_token=token,
            session=session,
            timeout=limit(ctx, "delete_seconds", 30),
        )
    else:
        suffix = "schedule" if operation == "schedule_read" else "notifications"
        response = call(
            ctx,
            "GET",
            f"/api/v1/workbooks/{target['workbookId']}/{suffix}",
            identity=identity,
            raw_token=token,
            session=session,
        )
    if status >= 400:
        code = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND"}[status]
        project_error(ctx, response, status, action, code)
        with setup(ctx):
            after = read_schedule(ctx, "main", ids, "Owner schedule after", identity=owner)
            notes_after = notifications(
                ctx, "main", ids, "Owner notifications after", identity=owner
            )
        if (after, notes_after) != (before, notes_before):
            raise CheckFailed(f"{action} was refused but changed the workbook's state.")
        return f"{action} returned {status} {code} with no data and no state change."
    if response.status_code >= 500:
        expect_status(response, status, action)
    ctx.qualify()
    expect_status(response, status, action)
    if operation == "delete":
        if response.content:
            raise CheckFailed("The owner's 204 deletion response has a body.")
        gone = call(ctx, "GET", version_path(ids), identity=owner)
        project_error(ctx, gone, 404, "Version after owner deletion", "NOT_FOUND")
        return "The owner's DELETE returned 204 with no body and the workbook is gone."
    if operation == "schedule_write":
        result = body_of(response, "ScheduleResponse", action)
        check_schedule(
            result,
            ids,
            action,
            enabled=True,
            interval=30,
            deadline="2027-09-30",
            next_at=now_of(ctx, "main") + 30 * DAY,
        )
        return "The owner's PUT returned 200 with the replaced configuration."
    definition = "ScheduleResponse" if operation == "schedule_read" else "NotificationResponse"
    body = body_of(response, definition, action)
    owners_view = before if operation == "schedule_read" else {"notifications": notes_before}
    if body != owners_view:
        raise CheckFailed(f"{action} differs from the owner's view.")
    return f"{action} returned 200 with the owner's read-only data."


# --- controlled failures (AC-14, AC-13, AC-16) ---------------------------------------------------


def fault_p5(ctx: SuiteContext) -> str:
    fault = str(ctx.parameters["fault"])
    if fault == "notification_failures":
        # This scenario arms its own single namespace (one armed counter per fault family).
        return _notification_fault(ctx, "Notification fault")
    session = namespace(ctx, f"Fault {fault}", {"faults": {fault: 1}})
    return FAULT_CASES[fault](ctx, session)


def _f_audit(ctx: SuiteContext, session: str) -> str:
    ids = upload_as(ctx, "clean_all_six", "Armed upload", session=session)
    failed = poll_audit(ctx, ids, "Armed audit", session=session, allow_failure=True)
    code, stage, message = INJECTED["audit_failures"]
    _expect_injected(failed, code, stage, message, "Injected audit failure")
    _, report = report_bytes(ctx, ids, ids["auditId"], "Failed audit report", session=session)
    if (report["status"], report["checksPerformed"], report["findings"]) != (
        "PROCESSING_FAILED",
        [],
        [],
    ):
        raise CheckFailed("The failed audit report shows checks or findings.")
    again = upload_as(ctx, "clean_all_six", "Second upload", session=session)
    audit = poll_audit(ctx, again, "Second audit", session=session)
    check_audit(ctx, audit, "clean_all_six", "Second audit")
    return (
        f"Namespace {session}: one TEST_AUDIT_FAILURE with a bounded report; the next upload "
        "audited normally."
    )


def _f_report(ctx: SuiteContext, session: str) -> str:
    ids = upload_as(ctx, "clean_all_six", "Armed upload", session=session)
    failed = poll_audit(ctx, ids, "Armed audit", session=session, allow_failure=True)
    code, stage, message = INJECTED["report_failures"]
    _expect_injected(failed, code, stage, message, "Injected report failure")
    _, report = report_bytes(ctx, ids, ids["auditId"], "Failure-report snapshot", session=session)
    if report["status"] != "PROCESSING_FAILED" or report["findings"]:
        raise CheckFailed("The failure-report snapshot is not a failed report without findings.")
    if sorted(report["checksPerformed"]) != sorted(ALL_CHECKS):
        raise CheckFailed("A report-finalization failure must record all six completed checks.")
    current = version(ctx, ids, "Version", session=session)
    if current["latestAuditId"] != ids["auditId"] or current["status"] != "PROCESSING_FAILED":
        raise CheckFailed("The version does not retain the failed audit as latest.")
    response = call(ctx, "POST", f"{version_path(ids)}/audits", session=session, json={})
    accepted = ok(ctx, response, 202, "ReauditResponse", "Recovery re-audit")
    audit = poll_audit(ctx, ids, "Recovery re-audit", audit_id=accepted["auditId"], session=session)
    check_audit(ctx, audit, "clean_all_six", "Recovery re-audit")
    return (
        f"Namespace {session}: report finalization failed once after all six checks; the "
        "re-audit recovered."
    )


def _f_remediation(ctx: SuiteContext, session: str) -> str:
    fixture, plan = "alt_supported", "alt"
    ids = upload_as(ctx, fixture, "Armed upload", session=session)
    audit = poll_audit(ctx, ids, "Armed audit", session=session)
    _, status = _remediate(ctx, session, ids, audit, fixture, plan, "Armed remediation")
    assert status is not None
    code, stage, message = INJECTED["remediation_failures"]
    error = status["error"] or {}
    if status["status"] != "PROCESSING_FAILED" or (
        error.get("code"),
        error.get("message"),
        error.get("details"),
    ) != (code, message, {"stage": stage, "testOnly": True}):
        raise CheckFailed(
            "The armed remediation did not end PROCESSING_FAILED with the exact envelope."
        )
    if status["outputVersionId"] or status["outputId"] or status["auditId"]:
        raise CheckFailed("The failed remediation published an output version, output or audit.")
    record = history(ctx, ids, "History after the failed remediation", session=session)
    if [v["versionId"] for v in record["versions"]] != [ids["versionId"]]:
        raise CheckFailed("The failed remediation created a workbook version.")
    source, _ = download(
        ctx, f"{version_path(ids)}/source", "Source after failure", session=session
    )
    if source != ctx.suite.bytes_of(fixture):
        raise CheckFailed("The source bytes changed after the failed remediation.")
    _, second = _remediate(ctx, session, ids, audit, fixture, plan, "Second remediation")
    assert second is not None
    if second["status"] != "REMEDIATED":
        raise CheckFailed(
            f"The second remediation ended {second['status']}; the fault was not one-shot."
        )
    return (
        f"Namespace {session}: TEST_REMEDIATION_FAILURE published nothing; the next remediation "
        "succeeded."
    )


def _f_storage(ctx: SuiteContext, session: str) -> str:
    fixture = "clean_all_six"
    response = call(
        ctx,
        "POST",
        "/api/v1/workbooks",
        session=session,
        files=multipart(ctx.suite.bytes_of(fixture), ctx.suite.accepted(fixture).upload_name),
    )
    if response.status_code == 202:
        ctx.qualify()
        raise CheckFailed(
            "The armed upload was accepted instead of failing with TEST_STORAGE_FAILURE."
        )
    ctx.qualify()
    expect_status(response, 500, "Armed upload")
    error = body_of(response, "ProjectError", "Armed upload")
    if error["code"] != "TEST_STORAGE_FAILURE":
        raise CheckFailed(
            f"The armed upload returned {bounded(error['code'], 64)}, not TEST_STORAGE_FAILURE."
        )
    ids = upload_as(ctx, fixture, "Upload after the storage fault", session=session)
    poll_audit(ctx, ids, "Audit after the storage fault", session=session)
    record = history(ctx, ids, "History", session=session)
    if len(record["versions"]) != 1 or len(record["audits"]) != 1:
        raise CheckFailed("The failed upload left a version or audit behind.")
    new = upload_as(
        ctx,
        fixture,
        "New version after the storage fault",
        session=session,
        workbook_id=ids["workbookId"],
    )
    poll_audit(ctx, new, "New-version audit", session=session)
    return (
        f"Namespace {session}: one upload failed 500 TEST_STORAGE_FAILURE with nothing "
        "accepted; later uploads succeeded."
    )


def _f_scheduled(ctx: SuiteContext, session: str) -> str:
    ids, _ = audited_in(ctx, "clean_all_six", session, "Scheduled-fault workbook")
    return _scheduled_failure_then_recovery(ctx, session, ids, "Scheduled fault")


def _f_deletion(ctx: SuiteContext, session: str) -> str:
    ids, _ = audited_in(ctx, "clean_all_six", session, "Deletion-fault workbook")
    return _delete_fail_once(ctx, session, ids, "clean_all_six")


FAULT_CASES: dict[str, Callable[[SuiteContext, str], str]] = {
    "audit_failures": _f_audit,
    "report_failures": _f_report,
    "remediation_failures": _f_remediation,
    "storage_failures": _f_storage,
    "scheduled_audit_failures": _f_scheduled,
    "deletion_failures": _f_deletion,
}


def fault_reject(ctx: SuiteContext) -> str:
    case = str(ctx.parameters["case"])
    if case in ("unknown", "boolean", "empty", "combination"):
        faults = {
            "unknown": {"arbitrary_exception": 1},
            "boolean": {"audit_failures": True},
            "empty": {},
            "combination": {"audit_failures": 1, "report_failures": 1},
        }[case]
        session_id, response = setup_session(ctx, {"faults": faults})
        testing_error(ctx, response, 400, f"Fault setup {json.dumps(faults)}", "system")
        unknown_session(ctx, session_id, f"Fault setup {json.dumps(faults)}")
        return (
            f"Fault setup {json.dumps(faults)} returned 400 source system and created no namespace."
        )
    if case == "disabled":
        session_id, response = setup_session(ctx, {"faults": {"audit_failures": 1}})
        if response.status_code == 201:
            raise Prerequisite(
                f"Not executed: this deployment enables the fault capability (namespace "
                f"{session_id} was created), so the disabled-capability 403 is unobservable here."
            )
        testing_error(ctx, response, 403, "Fault setup with the capability disabled", None)
        unknown_session(ctx, session_id, "Disabled fault capability")
        return (
            "Valid fault setup with the capability disabled returned testing 403 and no namespace."
        )
    if case == "reused_id":
        session = namespace(ctx, "Reused fault ID", {"faults": {"audit_failures": 1}})
        _, response = setup_session(ctx, {"faults": {"audit_failures": 1}}, session_id=session)
        testing_error(ctx, response, 409, "Repeated setup of the armed session ID", None)
        return _one_shot(ctx, session, "Reused fault ID")
    if case == "no_consume":
        session = namespace(ctx, "Fault not consumed", {"faults": {"audit_failures": 1}})
        response = call(
            ctx,
            "POST",
            "/api/v1/workbooks",
            identity=None,
            session=session,
            files=multipart(ctx.suite.bytes_of("clean_all_six"), "clean.xlsx"),
        )
        project_error(
            ctx, response, 401, "Unauthenticated upload in the armed namespace", "UNAUTHORIZED"
        )
        rejected = ctx.suite.rejected("corrupt")
        response = call(
            ctx,
            "POST",
            "/api/v1/workbooks",
            session=session,
            files=multipart(ctx.suite.bytes_of("corrupt"), rejected.upload_name),
        )
        project_error(
            ctx,
            response,
            rejected.http_status,
            "Malformed upload in the armed namespace",
            rejected.error_code,
        )
        return _one_shot(ctx, session, "Fault not consumed")
    if case in ("ordinary", "other_session"):
        session = namespace(ctx, f"Fault isolation {case}", {"faults": {"audit_failures": 1}})
        target: SessionChoice = (
            "none" if case == "ordinary" else namespace(ctx, "Unarmed namespace")
        )
        ids = upload_as(ctx, "clean_all_six", f"Upload in the {case} namespace", session=target)
        try:
            audit = poll_audit(ctx, ids, f"Audit in the {case} namespace", session=target)
            check_audit(ctx, audit, "clean_all_six", f"Audit in the {case} namespace")
        finally:
            if case == "ordinary":
                with setup(ctx):
                    call(
                        ctx,
                        "DELETE",
                        f"/api/v1/workbooks/{ids['workbookId']}",
                        session="none",
                        timeout=limit(ctx, "delete_seconds", 30),
                    )
        return _one_shot(ctx, session, f"Fault isolation {case}")
    raise KeyError(case)


def _one_shot(ctx: SuiteContext, session: str, label: str) -> str:
    """The armed counter was not consumed earlier and fires exactly once now."""
    ids = upload_as(ctx, "clean_all_six", f"{label} armed upload", session=session)
    failed = poll_audit(ctx, ids, f"{label} armed audit", session=session, allow_failure=True)
    code, stage, message = INJECTED["audit_failures"]
    _expect_injected(failed, code, stage, message, f"{label} armed audit")
    again = upload_as(ctx, "clean_all_six", f"{label} next upload", session=session)
    check_audit(
        ctx,
        poll_audit(ctx, again, f"{label} next audit", session=session),
        "clean_all_six",
        f"{label} next audit",
    )
    return (
        f"{label}: namespace {session}'s fault fired exactly once, on its own first eligible "
        "upload."
    )


# --- deletion (AC-16, C12) ------------------------------------------------------------------------


def _rich_workbook(ctx: SuiteContext, session: str, label: str) -> dict[str, Any]:
    """Two uploads, a generated output, reports, a schedule and a reminder record."""
    fixture, plan = "alt_supported", "alt"
    ids, audit = audited_in(ctx, fixture, session, f"{label} first version")
    second, _ = audited_in(
        ctx, "preservation_rich", session, f"{label} second version", workbook_id=ids["workbookId"]
    )
    with setup(ctx):
        # The newest version (preservation_rich) has unresolved findings, so it is eligible.
        configure(
            ctx,
            session,
            ids,
            {"enabled": True, "intervalDays": 30, "complianceDeadline": DEADLINE},
            f"{label} schedule",
            next_at=now_of(ctx, session) + 30 * DAY,
            version_id=second["versionId"],
        )
        advance(
            ctx, session, threshold(DEADLINE, 90), f"{label} reminder", expected_notifications=1
        )
        response = call(ctx, "POST", f"{version_path(ids)}/audits", session=session, json={})
        reaudit = ok(ctx, response, 202, "ReauditResponse", "Re-audit of the first version")
        audit = poll_audit(ctx, ids, "Re-audit", audit_id=reaudit["auditId"], session=session)
        _, status = _remediate(ctx, session, ids, audit, fixture, plan, f"{label} remediation")
        assert status is not None
        if status["status"] != "REMEDIATED":
            raise CheckFailed(f"{label}: the preparatory remediation ended {status['status']}.")
        output_audit = poll_scheduled(
            ctx, session, ids, status["auditId"], status["outputVersionId"], "Output audit"
        )
    return {
        "ids": ids,
        "second": second,
        "reaudit": reaudit["auditId"],
        "remediation": status,
        "output_audit": output_audit["auditId"],
    }


def _known_urls(rich: dict[str, Any]) -> list[tuple[str, str]]:
    ids, second, status = rich["ids"], rich["second"], rich["remediation"]
    base = f"/api/v1/workbooks/{ids['workbookId']}"
    out = status["outputVersionId"]
    urls = [
        ("history", f"{base}/history"),
        ("schedule", f"{base}/schedule"),
        ("notifications", f"{base}/notifications"),
        ("output download", f"/api/v1/outputs/{status['outputId']}/workbook"),
    ]
    for version_id, audit_id in (
        (ids["versionId"], ids["auditId"]),
        (ids["versionId"], rich["reaudit"]),
        (second["versionId"], second["auditId"]),
        (out, rich["output_audit"]),
    ):
        urls += [
            ("version", f"{base}/versions/{version_id}"),
            ("source", f"{base}/versions/{version_id}/source"),
            ("audit", f"{base}/versions/{version_id}/audits/{audit_id}"),
            ("review", f"{base}/versions/{version_id}/audits/{audit_id}/review"),
            ("report", f"{base}/versions/{version_id}/audits/{audit_id}/report"),
        ]
    return list(dict.fromkeys(urls))


def _delete(
    ctx: SuiteContext, session: SessionChoice, workbook_id: str, *, identity: Identity = "userA"
) -> httpx.Response:
    return call(
        ctx,
        "DELETE",
        f"/api/v1/workbooks/{workbook_id}",
        identity=identity,
        session=session,
        timeout=limit(ctx, "delete_seconds", 30),
    )


def _expect_deleted(ctx: SuiteContext, response: httpx.Response, action: str) -> None:
    if response.status_code >= 500:
        expect_status(response, 204, action)
    ctx.qualify()
    expect_status(response, 204, action)
    if response.content:
        raise CheckFailed(f"{action}: the 204 response has a body.")


def deletion(ctx: SuiteContext) -> str:
    return DELETE_CASES[str(ctx.parameters["case"])](ctx)


def _d_complete(ctx: SuiteContext) -> str:
    session = namespace(ctx, "Complete deletion")
    rich = _rich_workbook(ctx, session, "Complete deletion")
    ids = rich["ids"]
    _expect_deleted(ctx, _delete(ctx, session, ids["workbookId"]), "Owner deletion")
    for label, path in (
        ("history", f"/api/v1/workbooks/{ids['workbookId']}/history"),
        ("schedule", f"/api/v1/workbooks/{ids['workbookId']}/schedule"),
        ("notifications", f"/api/v1/workbooks/{ids['workbookId']}/notifications"),
        ("source", f"{version_path(ids)}/source"),
        ("output", f"/api/v1/outputs/{rich['remediation']['outputId']}/workbook"),
    ):
        project_error(
            ctx, call(ctx, "GET", path, session=session), 404, f"Deleted {label}", "NOT_FOUND"
        )
    return (
        "Owner DELETE returned 204 with no body; source, output, history, schedule and "
        "notifications are gone."
    )


def _d_old_urls(ctx: SuiteContext) -> str:
    session = namespace(ctx, "Old URLs")
    rich = _rich_workbook(ctx, session, "Old URLs")
    urls = _known_urls(rich)
    with setup(ctx):
        response = _delete(ctx, session, rich["ids"]["workbookId"])
        expect_status(response, 204, "Owner deletion")
    for label, path in urls:
        project_error(
            ctx, call(ctx, "GET", path, session=session), 404, f"Deleted {label} URL", "NOT_FOUND"
        )
    return (
        f"All {len(urls)} previously valid workbook/version/audit/output/review/report URLs "
        "return 404."
    )


def _d_repeat(ctx: SuiteContext) -> str:
    session = namespace(ctx, "Repeat deletion")
    ids, _ = audited_in(ctx, "seeded_all_six", session, "Repeat deletion")
    with setup(ctx):
        expect_status(_delete(ctx, session, ids["workbookId"]), 204, "First deletion")
    project_error(
        ctx, _delete(ctx, session, ids["workbookId"]), 404, "Repeated deletion", "NOT_FOUND"
    )
    return "A repeated DELETE of a deleted workbook returned 404 NOT_FOUND."


def _d_cancel_future(ctx: SuiteContext) -> str:
    session = namespace(ctx, "Cancel future work")
    start = now_of(ctx, session)
    doomed, _ = audited_in(ctx, "seeded_all_six", session, "Deleted workbook")
    control, _ = audited_in(ctx, "seeded_all_six", session, "Control workbook")
    body = {"enabled": True, "intervalDays": 1, "complianceDeadline": DEADLINE}
    with setup(ctx):
        configure(ctx, session, doomed, body, "Deleted workbook schedule", next_at=start + DAY)
        configure(ctx, session, control, body, "Control schedule", next_at=start + DAY)
    _expect_deleted(ctx, _delete(ctx, session, doomed["workbookId"]), "Owner deletion")
    clock = advance(
        ctx,
        session,
        threshold(DEADLINE, 90),
        "Advance past due and threshold",
        expected_audits=1,
        expected_notifications=1,
    )
    control_audits = {a["auditId"] for a in audits_of(ctx, session, control, "Control history")}
    control_notes = {
        n["notificationId"] for n in notifications(ctx, session, control, "Control notifications")
    }
    if (
        set(clock["triggeredAudits"]) - control_audits
        or set(clock["triggeredNotifications"]) - control_notes
    ):
        raise CheckFailed("The clock created an audit or reminder for the deleted workbook.")
    return "After deletion no audit or reminder ran for the workbook; the control workbook's did."


def _d_other_data(ctx: SuiteContext) -> str:
    session = namespace(ctx, "Other data")
    doomed, _ = audited_in(ctx, "seeded_all_six", session, "Deleted workbook")
    kept, kept_audit = audited_in(ctx, "clean_all_six", session, "Kept workbook")
    main_ids = _main_base(ctx, "other-data")
    with setup(ctx):
        kept_history = history(ctx, kept, "Kept history before", session=session)
        main_history = history(ctx, main_ids, "Main history before")
    _expect_deleted(ctx, _delete(ctx, session, doomed["workbookId"]), "Owner deletion")
    if history(ctx, kept, "Kept history after", session=session) != kept_history:
        raise CheckFailed("Deleting one workbook changed another workbook's history.")
    source, _ = download(ctx, f"{version_path(kept)}/source", "Kept source", session=session)
    if source != ctx.suite.bytes_of("clean_all_six"):
        raise CheckFailed("Deleting one workbook changed another workbook's bytes.")
    if history(ctx, main_ids, "Main history after") != main_history:
        raise CheckFailed("Deleting a workbook changed another namespace's data.")
    del kept_audit
    return "Another workbook in the namespace and the main namespace's data were unchanged."


def _d_retain_tester(ctx: SuiteContext) -> str:
    digests = ctx.state.get("tester_report_digests")
    if not callable(digests):
        raise Prerequisite("Not executed: this run has no access to finalized tester reports.")
    session = namespace(ctx, "Retain tester reports")
    ids, _ = audited_in(ctx, "seeded_all_six", session, "Deleted workbook")
    before = digests()
    _expect_deleted(ctx, _delete(ctx, session, ids["workbookId"]), "Owner deletion")
    if digests() != before:
        raise CheckFailed(
            "Finalized independent tester report bytes changed after a target deletion."
        )
    return (
        f"{len(before)} finalized tester report(s) for this target kept identical bytes after "
        "deletion."
    )


def _d_active(ctx: SuiteContext, kind: str) -> str:
    session = namespace(ctx, f"Delete during {kind}")
    if kind == "remediation":
        fixture = "alt_supported"
        ids, audit = audited_in(ctx, fixture, session, "Remediation source")
    with setup(ctx):
        if kind == "audit":
            ids = upload_as(ctx, "large_preservation", "Upload", session=session)
            active = ("audit", ids["auditId"])
        else:
            accepted, _ = _remediate(ctx, session, ids, audit, fixture, "alt", "Active", wait=False)
            active = ("remediation", accepted["remediationId"])
        configure(
            ctx,
            session,
            ids,
            {"enabled": True, "intervalDays": 5},
            "Schedule",
            next_at=now_of(ctx, session) + 5 * DAY,
        )
    response = _delete(ctx, session, ids["workbookId"])
    if response.status_code == 204:
        raise Prerequisite(
            f"Not executed: the {kind} finished before the DELETE arrived, so the active window "
            "was not observable (the workbook was deleted normally)."
        )
    project_error(ctx, response, 409, f"DELETE during an active {kind}", "ACTIVE_OPERATION")
    source = call(ctx, "GET", f"{version_path(ids)}/source", session=session, binary=True)
    expect_status(source, 200, "Source after the refused DELETE")
    kept = read_schedule(ctx, session, ids, "Schedule after the refused DELETE")
    if (kept["enabled"], kept["intervalDays"]) != (True, 5):
        raise CheckFailed("The refused DELETE changed the schedule configuration.")
    with setup(ctx):
        if active[0] == "audit":
            poll_audit(ctx, ids, "Active audit", session=session)
        else:
            status = poll_remediation(ctx, active[1], "Active remediation", session=session)
            if status["auditId"]:
                poll_scheduled(
                    ctx, session, ids, status["auditId"], status["outputVersionId"], "Output audit"
                )
        expect_status(_delete(ctx, session, ids["workbookId"]), 204, "Deletion after completion")
    return f"DELETE during an active {kind} returned 409 ACTIVE_OPERATION and changed nothing."


def _delete_fail_once(ctx: SuiteContext, session: str, ids: Mapping[str, str], fixture: str) -> str:
    response = _delete(ctx, session, ids["workbookId"])
    project_error(ctx, response, 500, "Armed DELETE", "TEST_DELETION_FAILURE")
    source, _ = download(
        ctx, f"{version_path(ids)}/source", "Source after the failed DELETE", session=session
    )
    if source != ctx.suite.bytes_of(fixture):
        raise CheckFailed("The source bytes are no longer readable after the pre-delete failure.")
    read_schedule(ctx, session, ids, "Schedule after the failed DELETE")
    _expect_deleted(ctx, _delete(ctx, session, ids["workbookId"]), "Owner retry")
    project_error(
        ctx,
        call(ctx, "GET", version_path(ids), session=session),
        404,
        "Version after retry",
        "NOT_FOUND",
    )
    return (
        f"Namespace {session}: TEST_DELETION_FAILURE left the workbook readable; the owner "
        "retry completed 204."
    )


def _d_fail_once(ctx: SuiteContext) -> str:
    session = namespace(ctx, "Deletion fails once", {"faults": {"deletion_failures": 1}})
    ids, _ = audited_in(ctx, "seeded_all_six", session, "Deletion-fault workbook")
    return _delete_fail_once(ctx, session, ids, "seeded_all_six")


DELETE_CASES: dict[str, Callable[[SuiteContext], str]] = {
    "complete": _d_complete,
    "old_urls": _d_old_urls,
    "repeat": _d_repeat,
    "cancel_future": _d_cancel_future,
    "other_data": _d_other_data,
    "retain_tester": _d_retain_tester,
    "active_audit": lambda ctx: _d_active(ctx, "audit"),
    "active_remediation": lambda ctx: _d_active(ctx, "remediation"),
    "fail_once": _d_fail_once,
}


# --- the tester's own report (C6 only where a target report is evaluated) ------------------------


def _provisional(ctx: SuiteContext) -> tuple[Json, bytes]:
    from tester.suite.runner import build_report, interrupted_results

    results = interrupted_results(
        ctx.suite, list(ctx.results), "Provisional self-check placeholder."
    )
    report = build_report(
        ctx.suite, ctx.session_id, "https://provisional.invalid", results, ctx.code, ctx.markers
    )
    raw = report.model_dump_json().encode("utf-8")
    return json.loads(raw), raw


def tester_report(ctx: SuiteContext) -> str:
    from tester.phase5.profile import RunProfile, check_report_profile

    case = str(ctx.parameters["case"])
    profile = RunProfile.restore(dict(ctx.suite.profile or {}))
    body, raw = _provisional(ctx)
    if case == "code_copy":
        from tester.suite.runner import retrieve_code_coverage

        response = ctx.client.request(
            "GET", "/test/report", identity="test-admin", params={"session_id": ctx.session_id}
        )
        report = ok(ctx, response, 200, "SystemReport", "System coverage report")
        if report["session_id"] != ctx.session_id:
            raise CheckFailed("The system report names a different session.")
        # A report read changes no measurement, so the tester's copy must equal it exactly.
        copied = retrieve_code_coverage(ctx)
        if copied.model_dump(mode="json") != report["coverage"]["code"]:
            raise CheckFailed(
                "The copied code measure differs from the target's session report "
                f"({bounded(copied.basis, 100)})."
            )
        ctx.code = copied
        return (
            "The suite code measure is an exact copy of the target's session report "
            f"({bounded(copied.basis, 100)})."
        )
    ctx.qualify(components=[])
    if case == "self_model":
        if profile.kind != "own":
            raise Prerequisite(
                "Not executed: this is a cross-team session; the own-team model profile does "
                "not apply."
            )
        check_report_profile(body, "own")
        model = body["coverage"]["model"]
        if model["total"] != len(profile.requirements) or not model["total"]:
            raise CheckFailed(
                "Model coverage total is not the frozen design map's requirement count."
            )
        design = profile.design_map or {}
        if (
            design["designVersion"] not in model["basis"]
            or design["mappingUrl"] not in model["basis"]
        ):
            raise CheckFailed("Model basis omits the frozen design version or mapping link.")
        return (
            f"Own-team report carries model coverage over all {model['total']} design requirements."
        )
    if case == "cross_omit":
        if profile.kind != "cross":
            raise Prerequisite(
                "Not executed: this is an own-team session; the cross-team omission does not apply."
            )
        check_report_profile(body, "cross")
        if b'"model"' in raw:
            raise CheckFailed("The cross-team report serializes a model key.")
        return "Cross-team report has no coverage.model key at all."
    if case == "system_18":
        system = body["coverage"]["system"]
        if system["total"] != 18 or not 0 <= system["covered"] <= 17:
            raise CheckFailed("System coverage is not counted over AC-01 to AC-18 without AC-17.")
        return (
            f"System coverage is {system['covered']}/18 so far; AC-17 is never claimed over HTTP."
        )
    if case == "component_12":
        component = body["coverage"]["component"]
        if component["total"] != 12 or not 0 <= component["covered"] <= 11:
            raise CheckFailed("Component coverage is not counted over C1-C12 without C9.")
        return (
            f"Component coverage is {component['covered']}/12 so far; C9 is never claimed over "
            "HTTP."
        )
    if case == "stored_bytes":
        from app.contracts.models import SuiteReport

        check_report_profile(body, profile.kind)
        again = SuiteReport.model_validate_json(raw).model_dump_json().encode("utf-8")
        if again != raw:
            raise CheckFailed("Serialized report bytes change on a parse/serialize round trip.")
        return (
            f"A {profile.kind}-team report round-trips byte-identically with its profile retained."
        )
    raise KeyError(case)


# --- real-time performance and repeatability (AC-18) ---------------------------------------------


def performance_run(ctx: SuiteContext) -> str:
    fixture = str(ctx.parameters["fixture"])
    run = int(str(ctx.parameters["run"]))
    accepted = ctx.suite.accepted(fixture)
    started = ctx.clock()
    response = call(
        ctx,
        "POST",
        "/api/v1/workbooks",
        files=multipart(ctx.suite.bytes_of(fixture), accepted.upload_name),
    )
    accepted_at = ctx.clock()
    upload_seconds = accepted_at - started
    ids: dict[str, str] = ok(ctx, response, 202, "UploadResponse", f"Performance upload {run}")
    fresh(ctx, [ids["workbookId"], ids["versionId"], ids["auditId"]], f"Performance upload {run}")
    observations: dict[int, Json] = ctx.state.setdefault("p5:performance", {})
    audit = poll_audit(ctx, ids, f"Performance audit {run}", allow_failure=True)
    elapsed = ctx.clock() - started
    processing = ctx.clock() - accepted_at
    record: Json = {
        "run": run,
        "upload_seconds": round(upload_seconds, 3),
        "accepted_to_terminal_seconds": round(processing, 3),
        "end_to_end_seconds": round(elapsed, 3),
        "status": audit["status"],
    }
    observations[run] = record
    if audit["status"] == "PROCESSING_FAILED":
        error = audit["error"] or {}
        code = str(error.get("code"))[:64]
        record["failure_code"] = code
        if not CONTROLLED_TIMEOUT.fullmatch(code):
            # Only a controlled timeout is AC-18's stated failure alternative. The 2.1.0 code
            # list is open ("include PROCESSING_TIMEOUT"), so any *_TIMEOUT code qualifies; any
            # other failure of a valid boundary workbook is an unexpected internal failure (§14).
            raise SystemFault(
                f"Run {run} ended PROCESSING_FAILED ({code}) after {processing:.1f} s; this "
                "is not the controlled timeout alternative."
            )
        record["controlled_failure"] = code
        return (
            f"Run {run}: controlled failure {record['controlled_failure']} after {elapsed:.1f} s "
            f"(upload {upload_seconds:.1f} s); a controlled failure is not a successful audit."
        )
    expected = check_audit(ctx, audit, fixture, f"Performance audit {run}")
    record["findings"] = [list(row) for row in expected]
    # Bounds apply from acceptance (15 s queue + 60 s engine); upload has its own 35 s bound.
    if processing > 75:
        raise CheckFailed(
            f"Run {run} took {processing:.1f} s from acceptance, beyond the 15 s queue + 60 s "
            "engine bounds."
        )
    return (
        f"Run {run}: exact findings in {elapsed:.1f} s end to end (upload {upload_seconds:.1f} s), "
        "tester-observed real time; target-side queue/engine split is separate evidence."
    )


def performance_compare(ctx: SuiteContext) -> str:
    observations: dict[int, Json] = ctx.state.get("p5:performance", {})
    successful = {run: obs for run, obs in observations.items() if "findings" in obs}
    if not successful:
        raise Prerequisite(
            "Not executed: no successful performance repetition was recorded to compare."
        )
    ctx.qualify()
    first = next(iter(successful.values()))["findings"]
    differing = [run for run, obs in successful.items() if obs["findings"] != first]
    summary = "; ".join(
        f"run {run}: {obs['status']} {obs['end_to_end_seconds']} s"
        for run, obs in sorted(observations.items())
    )
    if differing:
        raise CheckFailed(f"Successful runs {differing} returned different findings ({summary}).")
    return (
        f"{len(successful)} successful run(s) returned identical issue types and locations "
        f"({summary}; {5 - len(observations)} not recorded). Environment: tester-observed "
        "monotonic time over the registered HTTPS path."
    )


CHECKS: dict[str, Callable[[SuiteContext], str]] = {
    "schedule_default": schedule_default,
    "schedule_bound": schedule_bound,
    "schedule_replay": schedule_replay,
    "schedule_deadline_only": schedule_deadline_only,
    "schedule_interval_change": schedule_interval_change,
    "schedule_disable": schedule_disable,
    "schedule_reenable": schedule_reenable,
    "schedule_before_due": schedule_before_due,
    "schedule_exact_due": schedule_exact_due,
    "schedule_coalesce": schedule_coalesce,
    "schedule_same_time": schedule_same_time,
    "schedule_latest_upload": schedule_latest_upload,
    "schedule_latest_output": schedule_latest_output,
    "schedule_old_reaudit": schedule_old_reaudit,
    "schedule_busy": schedule_busy,
    "schedule_two_workbooks": schedule_two_workbooks,
    "schedule_failed_next": schedule_failed_next,
    "schedule_reject": schedule_reject,
    "reminder": reminder,
    "clock": clock,
    "clock_access": clock_access,
    "access_p5": access_p5,
    "fault_p5": fault_p5,
    "fault_reject": fault_reject,
    "deletion": deletion,
    "tester_report": tester_report,
    "performance_run": performance_run,
    "performance_compare": performance_compare,
}
