"""run_query: answers free-form questions with one read-only SQL statement.

Layers of protection (the database is the real lock; the checks here are for clear messages):
1. The connection logs in as finance_reader, which may SELECT only the views and the change log.
2. The statement runs in a READ ONLY transaction with a 3-second timeout.
3. It is sent as a prepared statement, so the database refuses more than one statement.
4. At most 500 rows and ~200 KB come back; money comes back as exact strings.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg import errors

from app.errors import UserError
from app.models import QueryColumn, QueryOut

MAX_ROWS = 500
MAX_BYTES = 200_000
MAX_SQL_LENGTH = 5_000

SCHEMA_CARD = """\
Views you can read (nothing else; one SELECT or WITH statement; 3 s; max 500 rows):
- v_spending(id, occurred_at, day, account_id, category_id, category, parent_category, top_category,
  category_kind, amount_eur, merchant, note, tags) — spending: expenses +, refunds −; loans never included.
  Use top_category for "how much on <parent>" (it covers the parent and its children).
  category_kind: regular (everyday, in the forecast) or structural (rent, travel, unrecorded money).
- v_income(id, occurred_at, day, account_id, category_id, category, amount_eur, merchant, note, tags)
  — real income only (borrowed money excluded).
- v_movements(transaction_id, occurred_at, day, account_id, type, category_id, other_account_id,
  amount_eur, units) — one line per effect on an account: + in, − out; a transfer is two lines.
  A balance on a date = sum of amount_eur from the account's opening up to that date.
- v_account_balances(account_id, name, unit, description, is_active, ledger_start, balance_eur,
  balance_units, net_invested_eur) — current balances (balance_eur only for EUR accounts).
- v_items(id, transaction_id, day, account_id, category_id, merchant, name_raw, name_clean, item_type,
  quantity, quantity_unit, price_eur, discount_eur, price_per_unit) — receipt lines of active purchases.
- v_accounts(id, name, description, unit, is_default_expense, is_default_income, is_active)
- v_categories(id, name, parent, kind, description, aliases, is_active)
- change_log(id, changed_at, entity, entity_id, action, old_values, new_values) — history of changes.
Rules: filter dates with the `day` column (the user's local date); voided transactions are already
excluded everywhere; do all sums and averages in SQL, never in your head; join v_accounts/v_categories
for names; amount_eur values come back as strings."""


def _plain(value: Any) -> Any:
    """Make values JSON-safe without losing precision: money and numbers become exact strings."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def run_query(conn: psycopg.Connection, sql_text: str) -> QueryOut:
    """conn must be a finance_reader connection (not autocommit)."""
    query = sql_text.strip().rstrip(";").strip()
    if not query:
        raise UserError("empty_query", "Give one SELECT statement.")
    if len(query) > MAX_SQL_LENGTH:
        raise UserError("query_too_long", "The query is too long; simplify it.")
    if query.split(None, 1)[0].lower() not in ("select", "with"):
        raise UserError("not_a_select", "Only one read-only SELECT (or WITH … SELECT) statement is allowed.")

    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '3s'")
        cur = conn.cursor()
        try:
            cur.execute(query, prepare=True)   # prepared: the database refuses a second statement
        except errors.QueryCanceled as e:
            raise UserError("timeout", "The query took longer than 3 seconds. Make it simpler or narrower "
                                       "(fewer rows, a shorter date range).") from e
        except errors.InsufficientPrivilege as e:
            raise UserError("not_allowed", "That table isn't readable. Use only the views listed in the "
                                           "run_query description.") from e
        except (errors.ReadOnlySqlTransaction, errors.FeatureNotSupported) as e:
            raise UserError("read_only", "Only reading is allowed here; changes go through the other tools.") from e
        except psycopg.Error as e:
            first_line = (str(e).splitlines() or ["database error"])[0]
            raise UserError("sql_error", f"The database said: {first_line}. Fix the SQL and try again.") from e
        if cur.description is None:
            raise UserError("not_a_select", "That statement returns no rows. Use a SELECT.")
        columns = [QueryColumn(name=d.name, type=_type_name(conn, d.type_code)) for d in cur.description]
        fetched = cur.fetchmany(MAX_ROWS + 1)

    rows, size, truncated = [], 0, len(fetched) > MAX_ROWS
    for raw in fetched[:MAX_ROWS]:
        row = [_plain(v) for v in raw]
        size += len(str(row))
        if size > MAX_BYTES:
            truncated = True
            break
        rows.append(row)
    return QueryOut(columns=columns, rows=rows, row_count=len(rows), truncated=truncated)


def _type_name(conn: psycopg.Connection, oid: int) -> str:
    info = conn.adapters.types.get(oid)
    return info.name if info else str(oid)
