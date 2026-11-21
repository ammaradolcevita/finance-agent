"""Accounts: list them with balances, add one (with its opening balance), and calculate a balance at a moment.

Services never commit: the tool layer runs one database transaction per tool call (db.tx).
"""

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from app.errors import UserError
from app.models import AccountChangeOut, AccountOut, AddAccountOut
from app.services.dates import check_when, resolve_when
from app.services.resolve import Candidate, resolve

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


def update_account(conn: psycopg.Connection, name: str, *, new_name: str | None = None,
                   description: str | None = None, default_for: str | None = None, archived: bool | None = None,
                   price_ref: str | None = None) -> AccountChangeOut:
    """Rename, describe, change defaults ('expense', 'income', 'both' or 'none'), archive or restore an account.
    The unit can't change once the account exists. Archiving is only allowed at balance 0 (euros and coins),
    so money never silently disappears from the totals. Every change goes to the change log."""
    rows = conn.execute("SELECT id, name, description, unit, is_default_expense, is_default_income, is_active, "
                        "price_ref FROM accounts").fetchall()
    m = resolve(name, [Candidate(r[0], r[1]) for r in rows], allow_fuzzy=False)
    if m.kind == "none":
        hint = f" Did you mean: {', '.join(m.suggestions)}?" if m.suggestions else ""
        raise UserError("unknown_account", f'No account called "{name}".{hint} Use the exact name.')
    aid, old_name, old_desc, unit, old_exp, old_inc, old_active, old_ref = next(r for r in rows if r[0] == m.candidate.id)
    old = {"name": old_name, "description": old_desc, "is_default_expense": old_exp, "is_default_income": old_inc,
           "is_active": old_active, "price_ref": old_ref}
    new = dict(old)
    warnings: list[str] = []

    if new_name is not None:
        new_name = " ".join(new_name.split())
        if not new_name:
            raise UserError("invalid_name", "The new name can't be empty.")
        if any(r[1].casefold() == new_name.casefold() and r[0] != aid for r in rows):
            raise UserError("name_taken", f"An account named '{new_name}' already exists.")
        new["name"] = new_name
    if description is not None:
        new["description"] = description.strip() or None
    if price_ref is not None:
        if unit == "EUR":
            raise UserError("invalid_price_ref", "price_ref is only for coin accounts.")
        new["price_ref"] = price_ref.strip() or None
    if default_for is not None:
        if default_for not in ("expense", "income", "both", "none"):
            raise UserError("invalid_default", "default_for must be expense, income, both or none.")
        if unit != "EUR" and default_for != "none":
            raise UserError("invalid_default", "A coin account can't be a default account.")
        new["is_default_expense"] = default_for in ("expense", "both")
        new["is_default_income"] = default_for in ("income", "both")
    if archived is not None:
        if archived:
            euros, coins = balance_at(conn, aid)
            if euros != 0 or coins != 0:
                shown = f"{coins} {unit}" if unit != "EUR" else f"{euros:.2f} €"
                raise UserError("not_empty", f"{old_name} still holds {shown}. Move the money out (a transfer) "
                                             "before archiving, so it doesn't disappear from the totals.")
            if new["is_default_expense"] or new["is_default_income"]:
                new["is_default_expense"] = new["is_default_income"] = False
                warnings.append("It was a default account; choose a new default account.")
        new["is_active"] = not archived

    changed = {k: v for k, v in new.items() if v != old[k]}
    if not changed:
        raise UserError("no_change", "Nothing to change: the values are the same as before.")
    if new["is_default_expense"] and not old_exp:
        conn.execute("UPDATE accounts SET is_default_expense = FALSE WHERE is_default_expense")
    if new["is_default_income"] and not old_inc:
        conn.execute("UPDATE accounts SET is_default_income = FALSE WHERE is_default_income")
    conn.execute(
        "UPDATE accounts SET name = %s, description = %s, is_default_expense = %s, is_default_income = %s, "
        "is_active = %s, price_ref = %s WHERE id = %s",
        (new["name"], new["description"], new["is_default_expense"], new["is_default_income"], new["is_active"],
         new["price_ref"], aid))
    conn.execute("INSERT INTO change_log (entity, entity_id, action, old_values, new_values) "
                 "VALUES ('account', %s, 'update', %s, %s)",
                 (aid, Jsonb({k: old[k] for k in changed}), Jsonb(changed)))
    account = next(a for a in list_accounts(conn, include_archived=True) if a.id == aid)
    return AccountChangeOut(account=account, warnings=warnings)
