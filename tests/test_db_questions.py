"""Step 5 on the practice branch: run_query safety and the accounts_overview money roadmap.
run_query uses its own read-only connection, so its tests use data-independent queries.
All names are made up. Skipped unless run with: uv run --env-file .env pytest"""

from datetime import timedelta
from decimal import Decimal as D

import pytest

from app.errors import UserError
from app.models import TransactionIn as T
from app.services import accounts as acc
from app.services.overview import accounts_overview
from app.services.query import run_query
from tests.helpers import AMS, record


# --- run_query ----------------------------------------------------------------------------

def test_reading_a_view_works_and_money_comes_back_as_exact_strings(reader_conn):
    out = run_query(reader_conn, "SELECT 2.40::numeric(12,2) AS amount, count(*) AS n FROM v_spending")
    assert [c.name for c in out.columns] == ["amount", "n"]
    assert out.rows[0][0] == "2.40"


@pytest.mark.parametrize("sql", [
    "INSERT INTO categories (name) VALUES ('x')",
    "DELETE FROM transactions",
    "UPDATE categories SET name = 'x'",
    "DROP TABLE drafts",
])
def test_anything_but_select_is_refused(reader_conn, sql):
    with pytest.raises(UserError) as e:
        run_query(reader_conn, sql)
    assert e.value.code == "not_a_select"


def test_a_write_hidden_inside_with_is_blocked_by_the_database(reader_conn):
    with pytest.raises(UserError) as e:
        run_query(reader_conn, "WITH gone AS (DELETE FROM transactions RETURNING id) SELECT count(*) FROM gone")
    assert e.value.code in ("not_allowed", "read_only")


def test_two_statements_are_refused_by_the_database(reader_conn):
    with pytest.raises(UserError) as e:
        run_query(reader_conn, "SELECT 1; DELETE FROM transactions")
    assert e.value.code == "sql_error" and "multiple commands" in e.value.message


def test_raw_tables_and_drafts_are_not_readable(reader_conn):
    for table in ("transactions", "drafts", "accounts"):
        with pytest.raises(UserError) as e:
            run_query(reader_conn, f"SELECT count(*) FROM {table}")
        assert e.value.code == "not_allowed"


def test_slow_queries_are_stopped(reader_conn):
    with pytest.raises(UserError) as e:
        run_query(reader_conn, "SELECT pg_sleep(5)")
    assert e.value.code == "timeout"


def test_big_answers_are_cut_at_500_rows(reader_conn):
    out = run_query(reader_conn, "SELECT g FROM generate_series(1, 1000) AS g")
    assert out.row_count == 500 and out.truncated


def test_percent_signs_in_text_work(reader_conn):
    out = run_query(reader_conn, "SELECT 'milk 1%' LIKE '%1\\%' ESCAPE '\\' AS ok")
    assert out.rows == [[True]]


# --- accounts_overview -----------------------------------------------------------------------

def test_money_roadmap_follows_the_money_and_checks_itself(world):
    conn, now = world
    record(conn, now, T(type="income", amount_eur=D("1000"), note="salary"))                 # into Main Bank
    record(conn, now, T(type="transfer", amount_eur=D("500"), account="Test Main Bank", to_account="Test Card"))
    record(conn, now, T(amount_eur=D("450"), category="Test Rent Omega", account="Test Main Bank"))
    record(conn, now, T(amount_eur=D("12.50"), category="Test Groceries Alpha"))              # from Test Card
    record(conn, now, T(type="income", amount_eur=D("300"), category="Test Loans Zeta"))      # borrowed

    period_start = (now - timedelta(hours=2)).date().isoformat()       # includes the openings an hour ago
    out = accounts_overview(conn, AMS, now, date_from=period_start)
    by_name = {a.account: a for a in out.accounts}
    main, card = by_name["Test Main Bank"], by_name["Test Card"]

    assert main.end_eur == D("600") + 1000 - 500 - 450 + 300
    assert card.end_eur == D("150") + 500 - D("12.50")
    assert {(f.kind, f.name) for f in main.money_out} >= {("transfer", "Test Card"), ("expense", "Test Rent Omega")}
    assert ("transfer", "Test Main Bank") in {(f.kind, f.name) for f in card.money_in}
    assert out.income_eur == D("1000")                 # borrowed 300 is not income
    assert out.spending_eur == D("462.50")             # 450 rent + 12.50 groceries; the transfer is not spending
    assert not out.warnings and len(out.checks) == 2
    assert any(f.source == "Test Main Bank" and f.target == "Test Card" and f.amount_eur == D("500")
               for f in out.flows)


def test_one_account_only(world):
    conn, now = world
    record(conn, now, T(amount_eur=D("3"), category="Test Groceries Alpha"))
    out = accounts_overview(conn, AMS, now, account="Test Card")
    assert [a.account for a in out.accounts] == ["Test Card"]


def test_bad_period_is_refused(world):
    conn, now = world
    with pytest.raises(UserError):
        accounts_overview(conn, AMS, now, date_from="2026-12-10", date_to="2026-12-01")


def test_revaluation_shows_as_market_gain(world):
    conn, now = world
    acc.add_account(conn, name="Test Broker", description=None, unit="EUR", opening_amount_eur=D("5000"),
                    opening_units=None, opening_date=None, opening_time=None, default_for=None, price_ref=None,
                    tz=AMS, now=now - timedelta(hours=1))
    record(conn, now, T(type="revaluation", account="Test Broker", new_value_eur=D("5300")))
    out = accounts_overview(conn, AMS, now, account="Test Broker")
    broker = out.accounts[0]
    assert ("revaluation", "market") in {(f.kind, f.name) for f in broker.money_in}
    assert broker.end_eur == D("5300") and not out.warnings
