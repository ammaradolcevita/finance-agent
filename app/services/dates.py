"""Turning the user's date words (already converted by Claude to YYYY-MM-DD) into exact moments, in their time zone.
Pure functions: no database, easy to test (midnight, summer/winter time)."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NOON = time(12, 0)
FUTURE_TOLERANCE = timedelta(minutes=10)
OLD_AFTER = timedelta(days=365)


def resolve_when(date_s: str | None, time_s: str | None, tz: ZoneInfo, now: datetime) -> tuple[datetime, bool]:
    """Returns (moment, exact). No date and no time = right now (exact). A date without a time = 12:00 local,
    so it never slips into the previous or next day in any time zone."""
    now_local = now.astimezone(tz)
    if date_s is None and time_s is None:
        return now_local, True
    d = date.fromisoformat(date_s) if date_s else now_local.date()
    t = time.fromisoformat(time_s) if time_s else NOON
    return datetime.combine(d, t, tzinfo=tz), time_s is not None


def check_when(when: datetime, now: datetime) -> tuple[str | None, str | None]:
    """Returns (error, warning). The future is an error; more than a year ago is a warning."""
    if when > now + FUTURE_TOLERANCE:
        return "That date/time is in the future. Purchases can only be recorded for now or the past.", None
    if now - when > OLD_AFTER:
        return None, "This date is more than a year ago: please double-check it."
    return None, None


def in_words(when: datetime, tz: ZoneInfo, exact: bool) -> str:
    """'Thu 3 Dec 2026' (or with the time, if a time was given), so the user can spot a wrong date."""
    local = when.astimezone(tz)
    text = f"{local:%a} {local.day} {local:%b %Y}"
    return f"{text}, {local:%H:%M}" if exact else text
