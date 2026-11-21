"""get_context: what Claude needs to know at the start of every chat."""

from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg

from app.models import AccountOut, CategoryOut, ContextOut


def fetch_categories(conn: psycopg.Connection) -> list[tuple]:
    """Reads active categories as (id, name, kind) rows, sorted by name."""
    return conn.execute(
        "SELECT id, name, kind FROM categories WHERE is_active ORDER BY lower(name)"
    ).fetchall()


def build_context(category_rows: list[tuple], timezone: ZoneInfo, now: datetime | None = None) -> ContextOut:
    """Pure function: rows + time zone (+ a fixed 'now' in tests) → ContextOut. No database."""
    now = (now or datetime.now(timezone)).astimezone(timezone)
    accounts: list[AccountOut] = []  # the accounts table arrives with migration 002 (step 4)
    notes = []
    if not accounts:
        notes.append("No accounts are set up yet: recording money is not possible until day-one setup.")
    return ContextOut(
        today=now.date(),
        weekday=now.strftime("%A"),
        timezone=timezone.key,
        accounts=accounts,
        categories=[CategoryOut(id=r[0], name=r[1], kind=r[2]) for r in category_rows],
        notes=notes,
    )


def get_context(conn: psycopg.Connection, timezone: ZoneInfo) -> ContextOut:
    return build_context(fetch_categories(conn), timezone)
