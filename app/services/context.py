"""get_context: what Claude needs to know at the start of every chat."""

from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg

from app.models import AccountOut, CategoryOut, ContextOut
from app.services.accounts import list_accounts


def fetch_categories(conn: psycopg.Connection) -> list[tuple]:
    """Active categories as (id, name, kind, parent name, description, aliases), sorted by name."""
    return conn.execute(
        "SELECT c.id, c.name, c.kind, p.name, c.description, c.aliases "
        "FROM categories c LEFT JOIN categories p ON p.id = c.parent_id "
        "WHERE c.is_active ORDER BY lower(c.name)"
    ).fetchall()


def build_context(category_rows: list[tuple], timezone: ZoneInfo, now: datetime | None = None,
                  accounts: list[AccountOut] | None = None) -> ContextOut:
    """Pure function: rows + accounts + time zone (+ a fixed 'now' in tests) → ContextOut. No database."""
    now = (now or datetime.now(timezone)).astimezone(timezone)
    accounts = accounts or []
    notes = []
    if not accounts:
        notes.append("No accounts are set up yet: offer the day-one setup before recording anything.")
    else:
        if not any(a.is_default_expense for a in accounts):
            notes.append("No default account for daily expenses: ask the user which account to use.")
        if not any(a.is_default_income for a in accounts):
            notes.append("No default account for income: ask the user which account to use.")
    categories = [
        CategoryOut(id=r[0], name=r[1], kind=r[2],
                    parent=r[3] if len(r) > 3 else None,
                    description=r[4] if len(r) > 4 else None,
                    aliases=list(r[5] or []) if len(r) > 5 else [])
        for r in category_rows
    ]
    return ContextOut(today=now.date(), weekday=now.strftime("%A"), timezone=timezone.key,
                      accounts=accounts, categories=categories, notes=notes)


def get_context(conn: psycopg.Connection, timezone: ZoneInfo) -> ContextOut:
    return build_context(fetch_categories(conn), timezone, accounts=list_accounts(conn))
