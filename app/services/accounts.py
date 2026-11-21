"""Accounts: list them with balances, add one (with its opening balance), and calculate a balance at a moment.

Services never commit: the tool layer runs one database transaction per tool call (db.tx).
"""

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg

from app.errors import UserError
from app.models import AccountOut, AddAccountOut
from app.services.dates import check_when, resolve_when
from app.services.resolve import Candidate

UNIT_RE = re.compile(r"^[A-Z0-9]{2,10}$")


@dataclass(frozen=True)
class AccountRow:
    id: int
    name: str
    unit: str
    is_default_expense: bool
    is_default_income: bool

    @property
    def is_coin(self) -> bool:
        return self.unit != "EUR"

    def candidate(self) -> Candidate:
        return Candidate(self.id, self.name)


def active_accounts(conn: psycopg.Connection) -> list[AccountRow]:
    rows = conn.execute(
        "SELECT id, name, unit, is_default_expense, is_default_income FROM accounts "
        "WHERE is_active ORDER BY lower(name)").fetchall()
    return [AccountRow(*r) for r in rows]


def list_accounts(conn: psycopg.Connection, include_archived: bool = False) -> list[AccountOut]:
    rows = conn.execute(
        "SELECT a.id, a.name, a.description, a.unit, a.is_default_expense, a.is_default_income, a.is_active,"
        "       b.balance_eur, b.balance_units, b.net_invested_eur "
        "FROM accounts a JOIN v_account_balances b ON b.account_id = a.id "
        "WHERE a.is_active OR %s ORDER BY lower(a.name)",
        (include_archived,),
    ).fetchall()
    keys = ["id", "name", "description", "unit", "is_default_expense", "is_default_income", "is_active",
            "balance_eur", "balance_units", "net_invested_eur"]
    return [AccountOut(**dict(zip(keys, r))) for r in rows]


def opening_moment(conn: psycopg.Connection, account_id: int) -> datetime | None:
    row = conn.execute(
        "SELECT occurred_at FROM transactions WHERE account_id = %s AND type = 'opening' AND status = 'active'",
        (account_id,)).fetchone()
    return row[0] if row else None


def balance_at(conn: psycopg.Connection, account_id: int, at: datetime | None = None) -> tuple[Decimal, Decimal]:
    """(euros, coins) of an account at a moment: everything from its opening up to `at` (default: now).
    Rows dated before the opening are already included in the stated opening balance, so they are skipped."""
    row = conn.execute(
        "SELECT COALESCE(sum(m.amount_eur), 0), COALESCE(sum(m.units), 0) FROM v_movements m "
        "WHERE m.account_id = %s"
        "  AND m.occurred_at >= COALESCE((SELECT occurred_at FROM transactions"
        "        WHERE account_id = %s AND type = 'opening' AND status = 'active'), '-infinity')"
        "  AND m.occurred_at <= COALESCE(%s::timestamptz, 'infinity'::timestamptz)",
        (account_id, account_id, at),
    ).fetchone()
    return Decimal(row[0]), Decimal(row[1])


def add_account(
    conn: psycopg.Connection,
    *,
    name: str,
    description: str | None,
    unit: str,
    opening_amount_eur: Decimal | None,
    opening_units: Decimal | None,
    opening_date: str | None,
    opening_time: str | None,
    default_for: str | None,
    price_ref: str | None,
    tz: ZoneInfo,
    now: datetime,
) -> AddAccountOut:
    name = " ".join(name.split())
    if not name:
        raise UserError("invalid_name", "The account needs a name.")
    unit = unit.strip().upper()
    if not UNIT_RE.match(unit):
        raise UserError("invalid_unit", "Unit must be EUR or a coin code of 2–10 letters/digits, e.g. BTC.")
    is_coin = unit != "EUR"
    if conn.execute("SELECT 1 FROM accounts WHERE lower(name) = lower(%s)", (name,)).fetchone():
        raise UserError("name_taken", f"An account named '{name}' already exists. Use another name.")
    if default_for not in (None, "expense", "income", "both"):
        raise UserError("invalid_default", "default_for must be expense, income or both.")
    if is_coin and default_for:
        raise UserError("invalid_default", "A coin account can't be the default for daily expenses or income.")
    if is_coin and opening_amount_eur is not None and opening_units is None:
        raise UserError("missing_units", f"How many {unit} does the account hold? Give opening_units.")
    if not is_coin and opening_units is not None:
        raise UserError("invalid_units", "opening_units is only for coin accounts.")

    warnings: list[str] = []
    expense_default = default_for in ("expense", "both")
    income_default = default_for in ("income", "both")
    if expense_default:
        conn.execute("UPDATE accounts SET is_default_expense = FALSE WHERE is_default_expense")
    if income_default:
        conn.execute("UPDATE accounts SET is_default_income = FALSE WHERE is_default_income")

    account_id = conn.execute(
        "INSERT INTO accounts (name, description, unit, price_ref, is_default_expense, is_default_income) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (name, description, unit, price_ref, expense_default, income_default),
    ).fetchone()[0]

    opening_id = None
    if opening_amount_eur is not None or opening_units is not None:
        when, _ = resolve_when(opening_date, opening_time, tz, now)
        error, warning = check_when(when, now)
        if error:
            raise UserError("invalid_date", error)
        if warning:
            warnings.append(warning)
        opening_id = conn.execute(
            "INSERT INTO transactions (occurred_at, type, amount_eur, account_id, units, unit, note, source) "
            "VALUES (%s, 'opening', %s, %s, %s, %s, %s, 'chat') RETURNING id",
            (when, opening_amount_eur if opening_amount_eur is not None else Decimal("0"), account_id,
             opening_units, unit if opening_units is not None else None, "opening balance"),
        ).fetchone()[0]
    else:
        warnings.append("No opening balance was given: the balance starts at 0. Add one only if the user has it.")

    account = next(a for a in list_accounts(conn, include_archived=True) if a.id == account_id)
    return AddAccountOut(account=account, opening_transaction_id=opening_id, warnings=warnings)
