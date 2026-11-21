"""The forecast baseline: simple, honest facts that Claude then reasons about freely.

    average per day = REGULAR spending on the account in the window ÷ days in the window
    window          = the last 60 days, but never before the account's opening (so no invented history)
    projected       = average per day × days left after today
    expected end    = balance − projected

Structural spending (rent, travel, unrecorded money) and loans are left out: they don't repeat daily.
Claude adds the user's context (what's at home, plans, habits) on top, and labels its own estimates.
"""

import calendar
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

import psycopg

from app.errors import UserError
from app.models import CategoryRate, ForecastOut
from app.services.accounts import balance_at, opening_moment
from app.services.resolve import Candidate, resolve

WINDOW_DAYS = 60
FEW_DAYS = 14
CENT = Decimal("0.01")


def _cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _pick_account(conn: psycopg.Connection, account: str | None) -> tuple[int, str, str]:
    rows = conn.execute("SELECT id, name, unit, is_default_expense FROM accounts WHERE is_active").fetchall()
    if account:
        m = resolve(account, [Candidate(r[0], r[1]) for r in rows], allow_fuzzy=False)
        if m.kind == "none":
            hint = f" Did you mean: {', '.join(m.suggestions)}?" if m.suggestions else ""
            raise UserError("unknown_account", f'No active account called "{account}".{hint}')
        row = next(r for r in rows if r[0] == m.candidate.id)
    else:
        row = next((r for r in rows if r[3]), None)
        if row is None:
            raise UserError("no_default", "No default spending account is set. Name the account.")
    if row[2] != "EUR":
        raise UserError("not_euro", "The forecast is for euro accounts (the account you spend from).")
    return row[0], row[1], row[2]


def forecast_month(conn: psycopg.Connection, tz: ZoneInfo, now: datetime, account: str | None = None,
                   balance_eur: Decimal | None = None) -> ForecastOut:
    account_id, name, _ = _pick_account(conn, account)
    today = now.astimezone(tz).date()
    last_day = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    days_left = (last_day - today).days          # from tomorrow: today's spending is already in the balance
    month_start = today.replace(day=1)

    opened = opening_moment(conn, account_id)
    window_from = today - timedelta(days=WINDOW_DAYS - 1)
    if opened is not None:
        window_from = max(window_from, opened.astimezone(tz).date())
    days_of_data = (today - window_from).days + 1

    rows = conn.execute(
        "SELECT category, parent_category, sum(amount_eur) FROM v_spending "
        "WHERE account_id = %s AND category_kind = 'regular' AND day BETWEEN %s AND %s "
        "GROUP BY category, parent_category ORDER BY sum(amount_eur) DESC",
        (account_id, window_from, today)).fetchall()
    by_category = [CategoryRate(category=r[0], parent=r[1], spent_in_window_eur=r[2],
                                avg_per_day_eur=_cents(Decimal(r[2]) / days_of_data)) for r in rows]
    window_total = sum((Decimal(r[2]) for r in rows), Decimal("0"))
    avg_per_day = window_total / days_of_data

    spent_all, spent_regular = conn.execute(
        "SELECT COALESCE(sum(amount_eur), 0), COALESCE(sum(amount_eur) FILTER (WHERE category_kind = 'regular'), 0) "
        "FROM v_spending WHERE account_id = %s AND day BETWEEN %s AND %s",
        (account_id, month_start, today)).fetchone()

    balance = balance_eur if balance_eur is not None else balance_at(conn, account_id)[0]
    projected = _cents(avg_per_day * days_left)

    notes = [f"Based on regular spending from {name} over {days_of_data} day(s) "
             f"({window_from} – {today}); rent, travel, unrecorded money and loans are not included."]
    if days_of_data < FEW_DAYS:
        notes.append(f"Only {days_of_data} day(s) of data: this baseline is rough. It becomes reliable after a few "
                     "weeks of use.")
    if not rows:
        notes.append("No regular spending recorded yet in this period, so the baseline projects 0.")
    if balance_eur is not None:
        notes.append("Uses the balance the user gave, not the recorded one.")

    return ForecastOut(
        account=name, today=today, balance_eur=_cents(balance), spent_this_month_eur=spent_all,
        spent_this_month_regular_eur=spent_regular, days_left=days_left, window_from=window_from,
        days_of_data=days_of_data, avg_per_day_eur=_cents(avg_per_day), by_category=by_category,
        projected_rest_of_month_eur=projected, expected_end_balance_eur=_cents(balance - projected), notes=notes)
