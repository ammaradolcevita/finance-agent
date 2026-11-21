"""Small helpers shared by the database tests. All names are made up."""

from datetime import timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from app.services import accounts as acc
from app.services import transactions as tx

AMS = ZoneInfo("Europe/Amsterdam")


def make_world(conn, now):
    """Two euro accounts (opened an hour ago) and a few made-up categories, inside the test's transaction."""
    start = now - timedelta(hours=1)
    common = dict(unit="EUR", opening_units=None, opening_date=None, opening_time=None, price_ref=None, tz=AMS)
    acc.add_account(conn, name="Test Main Bank", description="hub", opening_amount_eur=D("600"),
                    default_for="income", now=start, **common)
    acc.add_account(conn, name="Test Card", description="daily spending", opening_amount_eur=D("150"),
                    default_for="expense", now=start, **common)
    conn.execute("INSERT INTO categories (name, kind) VALUES ('Test Groceries Alpha', 'regular'), "
                 "('Test Loans Zeta', 'excluded'), ('Test Rent Omega', 'structural')")


def balance(conn, name):
    return {a.name: a for a in acc.list_accounts(conn, include_archived=True)}[name].balance_eur


def preview(conn, now, *txs, **kw):
    return tx.preview_transactions(conn, list(txs), AMS, now, **kw)


def record(conn, now, *txs):
    p = preview(conn, now, *txs)
    assert p.status == "ready", p.questions
    return tx.save_draft(conn, p.draft_id)
