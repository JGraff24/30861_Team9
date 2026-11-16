"""The tester's own reading of the Phase 5 calendar policy (specification 3.0.0 sections 6-9).

Written from the specification text with the standard library only. Nothing here imports or
mirrors the system's scheduler; `scripts/check_phase5_policy.py` and the unit tests check these
functions against every frozen vector in `phase5-policy-vectors.json`.
"""

import re
from datetime import UTC, date, datetime, timedelta

DAY = timedelta(days=1)
REMINDER_OFFSETS = (90, 30, 7)
DEFAULT_CLOCK = datetime(2027, 1, 1, tzinfo=UTC)
MIN_INTERVAL, MAX_INTERVAL = 1, 90
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(\d{1,6}))?Z")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_timestamp(value: str) -> datetime:
    """Strict Phase 5 Timestamp: UTC, ending Z, at most six fractional digits."""
    match = TIMESTAMP.fullmatch(value)
    if match is None:
        raise ValueError("not a Phase 5 UTC timestamp")
    whole = value[:19]
    parsed = datetime.strptime(whole, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    fraction = match.group(1)
    if fraction:
        parsed = parsed.replace(microsecond=int(fraction.ljust(6, "0")))
    return parsed


def format_timestamp(moment: datetime) -> str:
    """Canonical form the tester sends; comparisons always use parsed instants."""
    moment = moment.astimezone(UTC)
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        text += "." + f"{moment.microsecond:06d}".rstrip("0")
    return text + "Z"


def parse_date(value: str) -> date:
    if DATE.fullmatch(value) is None:
        raise ValueError("not YYYY-MM-DD")
    return datetime.strptime(value, "%Y-%m-%d").date()


def is_valid_interval(value: object) -> bool:
    """Strict integer 1-90; booleans are not integers here (section 4)."""
    return type(value) is int and MIN_INTERVAL <= value <= MAX_INTERVAL


def initial_due(now: datetime, interval_days: int) -> datetime:
    """First enable or a changed enabled/interval configuration: now + I * 86,400 s (6)."""
    return now + interval_days * DAY


def dispatch(due: datetime, interval_days: int, now: datetime) -> tuple[int, datetime]:
    """(audits accepted, next due) when the clock reaches `now` (section 7).

    Missed periods coalesce into one audit and the next due instant stays on the original
    grid: d + (floor((t - d) / I) + 1) * I, the first grid instant strictly after t.
    """
    if now < due:
        return 0, due
    interval = interval_days * DAY
    periods = (now - due) // interval
    return 1, due + (periods + 1) * interval


def reminder_instants(deadline: str | date) -> dict[int, datetime | None]:
    """00:00:00Z on the deadline date minus 90/30/7 calendar days (section 8).

    An instant before year 0001 is outside the observable timeline and is None.
    """
    day = parse_date(deadline) if isinstance(deadline, str) else deadline
    instants: dict[int, datetime | None] = {}
    for offset in REMINDER_OFFSETS:
        try:
            moment = day - timedelta(days=offset)
        except OverflowError:
            instants[offset] = None
            continue
        instants[offset] = datetime(moment.year, moment.month, moment.day, tzinfo=UTC)
    return instants


def eligible(latest_status: str | None, finding_count: int) -> bool:
    """Only a successful terminal audit with no findings suppresses a reminder (section 8)."""
    if latest_status in (None, "PROCESSING", "PROCESSING_FAILED"):
        return True
    return finding_count > 0


def crossed_offsets(
    configured_at: datetime,
    new_time: datetime,
    deadline: str,
    already_considered: set[int] | frozenset[int] = frozenset(),
) -> list[int]:
    """Offsets a processing pass at `new_time` must consider, in scheduledFor order.

    An instant counts when it lies in [configured_at, new_time] and its key was not already
    considered: no backfill before the deadline was configured, an instant equal to the
    configuration time is eligible on the next pass, and every crossed instant of a forward
    jump is evaluated once.
    """
    selected = []
    for offset, instant in reminder_instants(deadline).items():
        if instant is None or offset in already_considered:
            continue
        if configured_at <= instant <= new_time:
            selected.append((instant, offset))
    return [offset for _, offset in sorted(selected)]
