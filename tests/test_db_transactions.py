"""Step 4 on a real PostgreSQL (the practice branch), every test rolled back afterwards.
All names are made up. Skipped unless run with: uv run --env-file .env pytest"""

from datetime import timedelta
from decimal import Decimal as D

import pytest

from app.errors import UserError
from app.models import ItemIn, TransactionIn as T
from app.services import accounts as acc
from app.services import transactions as tx
from tests.helpers import AMS, balance, preview, record


# --- accounts ------------------------------------------------------------------------

def test_accounts_start_with_their_opening_balance(world):
    conn, _ = world
    accounts = {a.name: a for a in acc.list_accounts(conn)}
    assert accounts["Test Main Bank"].balance_eur == D("600")
    assert accounts["Test Card"].balance_eur == D("150")
    assert accounts["Test Card"].is_default_expense and accounts["Test Main Bank"].is_default_income


def test_second_opening_is_rejected(world):
    conn, now = world
    with pytest.raises(UserError) as e:
        preview(conn, now, T(type="opening", account="Test Card", amount_eur=D("10")))
    assert e.value.code == "opening_exists"


def test_account_names_are_unique_ignoring_case(world):
    conn, now = world
    with pytest.raises(UserError) as e:
        acc.add_account(conn, name="test card", description=None, unit="EUR", opening_amount_eur=None,
                        opening_units=None, opening_date=None, opening_time=None, default_for=None,
                        price_ref=None, tz=AMS, now=now)
    assert e.value.code == "name_taken"


# --- preview / save ------------------------------------------------------------------

def test_missing_amount_asks_and_saves_nothing(world):
    conn, now = world
    drafts_before = conn.execute("SELECT count(*) FROM drafts").fetchone()[0]
    p = preview(conn, now, T(category="Test Groceries Alpha"))
    assert p.status == "needs_info" and any("How much" in q for q in p.questions)
    assert conn.execute("SELECT count(*) FROM drafts").fetchone()[0] == drafts_before


def test_expense_uses_default_account_and_counts_only_after_save(world):
    conn, now = world
    p = preview(conn, now, T(amount_eur=D("2.40"), category="Test Groceries Alpha", note="was hungry",
                             tags=["Impulse"]))
    assert p.status == "ready"
    assert p.transactions[0].account == "Test Card"
    assert "#impulse" in p.transactions[0].summary
    assert balance(conn, "Test Card") == D("150")            # a preview changes nothing
    saved = tx.save_draft(conn, p.draft_id)
    assert balance(conn, "Test Card") == D("147.60")
    spent = conn.execute("SELECT amount_eur FROM v_spending WHERE id = %s", (saved.transaction_ids[0],)).fetchone()
    assert spent[0] == D("2.40")


def test_saving_twice_never_records_twice(world):
    conn, now = world
    p = preview(conn, now, T(amount_eur=D("5"), category="Test Groceries Alpha"))
    first = tx.save_draft(conn, p.draft_id)
    second = tx.save_draft(conn, p.draft_id)
    assert second.status == "already_saved" and second.transaction_ids == first.transaction_ids
    assert balance(conn, "Test Card") == D("145")


def test_expired_draft_is_rejected(world):
    conn, now = world
    p = preview(conn, now, T(amount_eur=D("5"), category="Test Groceries Alpha"))
    conn.execute("UPDATE drafts SET created_at = now() - interval '31 minutes' WHERE id = %s", (p.draft_id,))
    with pytest.raises(UserError) as e:
        tx.save_draft(conn, p.draft_id)
    assert e.value.code == "draft_expired"


def test_replaced_draft_cannot_be_saved(world):
    conn, now = world
    old = preview(conn, now, T(amount_eur=D("5"), category="Test Groceries Alpha"))
    new = preview(conn, now, T(amount_eur=D("6"), category="Test Groceries Alpha"), replaces_draft_id=old.draft_id)
    with pytest.raises(UserError) as e:
        tx.save_draft(conn, old.draft_id)
    assert e.value.code == "draft_replaced"
    assert tx.save_draft(conn, new.draft_id).status == "saved"


def test_batch_is_all_or_nothing_when_something_is_unclear(world):
    conn, now = world
    p = preview(conn, now, T(amount_eur=D("3"), category="Test Groceries Alpha"),
                T(amount_eur=D("4"), category="Something Totally Unknown Qwerty"))
    assert p.status == "needs_info" and p.questions[0].startswith("#2:")
    assert p.draft_id is None


def test_obvious_typo_in_category_is_shown_as_a_warning(world):
    conn, now = world
    p = preview(conn, now, T(amount_eur=D("3"), category="Test Groceries Alpah"))
    assert p.status == "ready"
    assert any('Interpreted category "Test Groceries Alpah" as "Test Groceries Alpha"' in w for w in p.warnings)


def test_future_date_is_rejected(world):
    conn, now = world
    tomorrow = (now + timedelta(days=1)).date().isoformat()
    with pytest.raises(UserError) as e:
        preview(conn, now, T(amount_eur=D("3"), category="Test Groceries Alpha", date=tomorrow))
    assert e.value.code == "invalid_date"


def test_amount_with_more_than_two_decimals_is_rejected(world):
    conn, now = world
    with pytest.raises(Exception):
        preview(conn, now, T(amount_eur=D("2.405"), category="Test Groceries Alpha"))


def test_duplicate_is_warned_not_blocked(world):
    conn, now = world
    record(conn, now, T(amount_eur=D("7.50"), category="Test Groceries Alpha"))
    p = preview(conn, now, T(amount_eur=D("7.50"), category="Test Groceries Alpha"))
    assert p.status == "ready" and any("Possible duplicate" in w for w in p.warnings)


def test_receipt_that_does_not_add_up_is_warned(world):
    conn, now = world
    items = [ItemIn(name_raw="MILK 1L", quantity=D("1"), quantity_unit="l", price_eur=D("1.20")),
             ItemIn(name_raw="BONUS", price_eur=D("-0.20"))]          # discount line is negative
    ok = preview(conn, now, T(amount_eur=D("1.00"), category="Test Groceries Alpha", items=items))
    assert not any("add up" in w for w in ok.warnings)
    bad = preview(conn, now, T(amount_eur=D("1.50"), category="Test Groceries Alpha", items=items))
    assert any("add up to 1.00" in w for w in bad.warnings)


# --- transfers, loans, time -----------------------------------------------------------

def test_transfer_moves_money_but_is_not_spending_or_income(world):
    conn, now = world
    saved = record(conn, now, T(type="transfer", amount_eur=D("100"), account="Test Main Bank",
                                to_account="Test Card"))
    assert balance(conn, "Test Main Bank") == D("500") and balance(conn, "Test Card") == D("250")
    tid = saved.transaction_ids[0]
    assert conn.execute("SELECT count(*) FROM v_spending WHERE id = %s", (tid,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM v_income WHERE id = %s", (tid,)).fetchone()[0] == 0


def test_borrowed_money_is_not_income(world):
    conn, now = world
    saved = record(conn, now, T(type="income", amount_eur=D("300"), category="Test Loans Zeta", note="from a friend"))
    assert balance(conn, "Test Main Bank") == D("900")       # the money is there…
    tid = saved.transaction_ids[0]
    assert conn.execute("SELECT count(*) FROM v_income WHERE id = %s", (tid,)).fetchone()[0] == 0   # …not income


def test_just_after_midnight_counts_on_the_new_day(world):
    conn, now = world
    today = now.date().isoformat()
    saved = record(conn, now, T(amount_eur=D("3"), category="Test Groceries Alpha", date=today, time="00:30"))
    day = conn.execute("SELECT day FROM v_spending WHERE id = %s", (saved.transaction_ids[0],)).fetchone()[0]
    assert day.isoformat() == today


# --- void and undo --------------------------------------------------------------------

def test_void_and_undo_restore_the_balance_and_are_logged(world):
    conn, now = world
    tid = record(conn, now, T(amount_eur=D("20"), category="Test Groceries Alpha")).transaction_ids[0]
    assert balance(conn, "Test Card") == D("130")
    tx.void_transaction(conn, tid, reason="entered twice")
    assert balance(conn, "Test Card") == D("150")
    tx.void_transaction(conn, tid, undo=True)
    assert balance(conn, "Test Card") == D("130")
    actions = [r[0] for r in conn.execute(
        "SELECT action FROM change_log WHERE entity = 'transaction' AND entity_id = %s ORDER BY id", (tid,))]
    assert actions == ["void", "undo_void"]


def test_void_needs_a_reason_and_opening_needs_confirmation(world):
    conn, now = world
    tid = record(conn, now, T(amount_eur=D("1"), category="Test Groceries Alpha")).transaction_ids[0]
    with pytest.raises(UserError) as e:
        tx.void_transaction(conn, tid, reason="")
    assert e.value.code == "missing_reason"
    opening = conn.execute("SELECT t.id FROM transactions t JOIN accounts a ON a.id = t.account_id "
                           "WHERE a.name = 'Test Card' AND t.type = 'opening'").fetchone()[0]
    with pytest.raises(UserError) as e:
        tx.void_transaction(conn, opening, reason="wrong")
    assert e.value.code == "confirm_opening"


def test_find_by_note_and_by_parent_category(world):
    conn, now = world
    parent = conn.execute("INSERT INTO categories (name, kind) VALUES ('Test Food Parent', 'regular') "
                          "RETURNING id").fetchone()[0]
    conn.execute("INSERT INTO categories (name, kind, parent_id) VALUES ('Test Ice Cream Child', 'regular', %s)",
                 (parent,))
    record(conn, now, T(amount_eur=D("2.40"), category="Test Ice Cream Child", note="vanilla cone"))
    assert len(tx.find_transactions(conn, text="vanilla").transactions) == 1
    found = tx.find_transactions(conn, category="Test Food Parent").transactions
    assert [t.category for t in found] == ["Test Ice Cream Child"]


# --- coins and revaluation --------------------------------------------------------------

def test_coin_account_buy_and_no_overselling(world):
    conn, now = world
    acc.add_account(conn, name="Test Coins", description="crypto", unit="btc", opening_amount_eur=D("1000"),
                    opening_units=D("0.5"), opening_date=None, opening_time=None, default_for=None,
                    price_ref=None, tz=AMS, now=now - timedelta(hours=1))
    record(conn, now, T(type="transfer", amount_eur=D("500"), account="Test Main Bank", to_account="Test Coins",
                        units=D("0.1")))
    coins = {a.name: a for a in acc.list_accounts(conn)}["Test Coins"]
    assert coins.unit == "BTC" and coins.balance_units == D("0.6") and coins.balance_eur is None
    assert coins.net_invested_eur == D("1500")
    with pytest.raises(UserError) as e:
        preview(conn, now, T(type="transfer", amount_eur=D("9000"), account="Test Coins",
                             to_account="Test Main Bank", units=D("1")))
    assert e.value.code == "not_enough_coins"


def test_revaluation_records_the_difference_and_detects_a_changed_balance(world):
    conn, now = world
    acc.add_account(conn, name="Test Broker", description="investments", unit="EUR", opening_amount_eur=D("5000"),
                    opening_units=None, opening_date=None, opening_time=None, default_for=None, price_ref=None,
                    tz=AMS, now=now - timedelta(hours=1))
    p = preview(conn, now, T(type="revaluation", account="Test Broker", new_value_eur=D("5300")))
    assert p.transactions[0].amount_eur == D("300")
    tx.save_draft(conn, p.draft_id)
    assert balance(conn, "Test Broker") == D("5300")

    stale = preview(conn, now, T(type="revaluation", account="Test Broker", new_value_eur=D("5400")))
    record(conn, now, T(type="transfer", amount_eur=D("100"), account="Test Main Bank", to_account="Test Broker"))
    with pytest.raises(UserError) as e:
        tx.save_draft(conn, stale.draft_id)
    assert e.value.code == "balance_changed"
