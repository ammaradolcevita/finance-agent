"""Step 4b on the practice branch (rolled back): fixing records, managing categories and accounts.
All names are made up. Skipped unless run with: uv run --env-file .env pytest"""

from datetime import timedelta
from decimal import Decimal as D

import pytest

from app.errors import UserError
from app.models import ItemIn, TransactionIn as T
from app.services import accounts as acc
from app.services import categories as cat
from app.services import transactions as tx
from tests.helpers import AMS, balance, record


def log_actions(conn, entity, entity_id):
    return [r[0] for r in conn.execute(
        "SELECT action FROM change_log WHERE entity = %s AND entity_id = %s ORDER BY id", (entity, entity_id))]


def cat_id(conn, name):
    return conn.execute("SELECT id FROM categories WHERE name = %s", (name,)).fetchone()[0]


# --- update_transaction ---------------------------------------------------------------

def test_fixing_the_amount_changes_the_balance_and_keeps_the_old_value(world):
    conn, now = world
    tid = record(conn, now, T(amount_eur=D("2.40"), category="Test Groceries Alpha")).transaction_ids[0]
    out = tx.update_transaction(conn, tid, AMS, now, amount_eur=D("2.80"))
    assert out.changed == ["amount_eur"] and out.transaction.amount_eur == D("2.80")
    assert balance(conn, "Test Card") == D("147.20")
    old, new = conn.execute("SELECT old_values, new_values FROM change_log WHERE entity = 'transaction' "
                            "AND entity_id = %s AND action = 'update'", (tid,)).fetchone()
    assert old == {"amount_eur": "2.40"} and new == {"amount_eur": "2.80"}


def test_moving_to_another_account_moves_the_money(world):
    conn, now = world
    tid = record(conn, now, T(amount_eur=D("50"), category="Test Groceries Alpha")).transaction_ids[0]
    tx.update_transaction(conn, tid, AMS, now, account="Test Main Bank")
    assert balance(conn, "Test Card") == D("150") and balance(conn, "Test Main Bank") == D("550")


def test_direct_fixes_need_the_exact_name(world):
    conn, now = world
    tid = record(conn, now, T(amount_eur=D("1"), category="Test Groceries Alpha")).transaction_ids[0]
    with pytest.raises(UserError) as e:
        tx.update_transaction(conn, tid, AMS, now, category="Test Groceries Alpah")   # typo: no guessing here
    assert e.value.code == "unknown_name" and "Test Groceries Alpha" in e.value.message


def test_cannot_fix_a_voided_transaction(world):
    conn, now = world
    tid = record(conn, now, T(amount_eur=D("1"), category="Test Groceries Alpha")).transaction_ids[0]
    tx.void_transaction(conn, tid, reason="mistake")
    with pytest.raises(UserError) as e:
        tx.update_transaction(conn, tid, AMS, now, amount_eur=D("2"))
    assert e.value.code == "is_void"


def test_moving_into_an_earlier_month_warns(world):
    conn, now = world
    tid = record(conn, now, T(amount_eur=D("1"), category="Test Groceries Alpha")).transaction_ids[0]
    earlier = (now.replace(day=1) - timedelta(days=3)).date().isoformat()
    out = tx.update_transaction(conn, tid, AMS, now, date=earlier)
    assert any("earlier month" in w for w in out.warnings)


def test_changing_a_receipt_total_warns_about_the_items(world):
    conn, now = world
    items = [ItemIn(name_raw="BREAD", price_eur=D("2.00")), ItemIn(name_raw="EGGS", price_eur=D("3.00"))]
    tid = record(conn, now, T(amount_eur=D("5.00"), category="Test Groceries Alpha", items=items)).transaction_ids[0]
    out = tx.update_transaction(conn, tid, AMS, now, amount_eur=D("6.00"))
    assert any("no longer matches" in w for w in out.warnings)


def test_clearing_a_note_and_replacing_tags(world):
    conn, now = world
    tid = record(conn, now, T(amount_eur=D("1"), category="Test Groceries Alpha", note="hungry",
                              tags=["impulse"])).transaction_ids[0]
    out = tx.update_transaction(conn, tid, AMS, now, note="", tags=["Healthy", "healthy "])
    assert out.transaction.note is None and out.transaction.tags == ["healthy"]


# --- categories ------------------------------------------------------------------------

def test_add_category_with_parent_and_aliases(world):
    conn, _ = world
    cat.add_category(conn, "Test Food Top", description="all food")
    out = cat.add_category(conn, "Test Bakery Child", parent="Test Food Top", aliases=["TBC"])
    assert out.category.parent == "Test Food Top" and out.category.aliases == ["TBC"]
    assert log_actions(conn, "category", out.category.id) == ["create"]


def test_only_two_levels(world):
    conn, _ = world
    cat.add_category(conn, "Test Food Top")
    cat.add_category(conn, "Test Bakery Child", parent="Test Food Top")
    with pytest.raises(UserError) as e:
        cat.add_category(conn, "Test Grandchild", parent="Test Bakery Child")
    assert e.value.code == "invalid_parent"


def test_names_and_aliases_never_clash(world):
    conn, _ = world
    cat.add_category(conn, "Test Coffee Place", aliases=["TCP"])
    with pytest.raises(UserError):
        cat.add_category(conn, "test coffee place")                 # same name, other case
    with pytest.raises(UserError):
        cat.add_category(conn, "Test Other", aliases=["tcp"])       # alias already used
    with pytest.raises(UserError):
        cat.add_category(conn, "TCP")                               # name equal to an alias


def test_rename_carries_all_history(world):
    conn, now = world
    tid = record(conn, now, T(amount_eur=D("4"), category="Test Groceries Alpha")).transaction_ids[0]
    cat.update_category(conn, "Test Groceries Alpha", new_name="Test Groceries Renamed")
    assert tx.load_transactions(conn, [tid])[0].category == "Test Groceries Renamed"


def test_archive_parent_requires_children_handled_first(world):
    conn, _ = world
    cat.add_category(conn, "Test Food Top")
    cat.add_category(conn, "Test Bakery Child", parent="Test Food Top")
    with pytest.raises(UserError) as e:
        cat.update_category(conn, "Test Food Top", archived=True)
    assert e.value.code == "has_children"
    cat.update_category(conn, "Test Bakery Child", archived=True)
    assert cat.update_category(conn, "Test Food Top", archived=True).is_active is False


def test_archived_category_is_not_offered_for_new_records(world):
    conn, now = world
    cat.update_category(conn, "Test Rent Omega", archived=True)
    p = tx.preview_transactions(conn, [T(amount_eur=D("1"), category="Test Rent Omega")], AMS, now)
    assert p.status == "needs_info"


def test_merge_moves_rows_keeps_old_name_as_alias_and_logs_ids(world):
    conn, now = world
    cat.add_category(conn, "Test Sweets")
    cat.add_category(conn, "Test Snacks")
    ids = [record(conn, now, T(amount_eur=D(a), category="Test Sweets")).transaction_ids[0] for a in ("1", "2")]
    out = cat.merge_categories(conn, "Test Sweets", "Test Snacks")
    assert out.moved_transactions == 2
    assert {t.category for t in tx.load_transactions(conn, ids)} == {"Test Snacks"}
    p = tx.preview_transactions(conn, [T(amount_eur=D("1"), category="test sweets")], AMS, now)
    assert p.transactions[0].category == "Test Snacks"       # the old word still works
    logged = conn.execute("SELECT new_values FROM change_log WHERE entity = 'category' AND entity_id = %s "
                          "AND action = 'merge'", (cat_id(conn, "Test Sweets"),)).fetchone()[0]
    assert sorted(logged["moved_transaction_ids"]) == sorted(ids)


def test_cannot_merge_a_parent_with_children(world):
    conn, _ = world
    cat.add_category(conn, "Test Food Top")
    cat.add_category(conn, "Test Bakery Child", parent="Test Food Top")
    with pytest.raises(UserError) as e:
        cat.merge_categories(conn, "Test Food Top", "Test Groceries Alpha")
    assert e.value.code == "has_children"


# --- accounts ----------------------------------------------------------------------------

def test_rename_and_describe_an_account(world):
    conn, _ = world
    out = acc.update_account(conn, "Test Card", new_name="Test Daily Card", description="groceries and coffee")
    assert out.account.name == "Test Daily Card" and out.account.description == "groceries and coffee"
    assert log_actions(conn, "account", out.account.id) == ["update"]


def test_switching_the_default_account(world):
    conn, _ = world
    acc.update_account(conn, "Test Main Bank", default_for="both")
    accounts = {a.name: a for a in acc.list_accounts(conn)}
    assert accounts["Test Main Bank"].is_default_expense and not accounts["Test Card"].is_default_expense


def test_archiving_needs_a_zero_balance(world):
    conn, now = world
    with pytest.raises(UserError) as e:
        acc.update_account(conn, "Test Card", archived=True)
    assert e.value.code == "not_empty"
    record(conn, now, T(type="transfer", amount_eur=D("150"), account="Test Card", to_account="Test Main Bank"))
    out = acc.update_account(conn, "Test Card", archived=True)
    assert out.account.is_active is False and any("default" in w for w in out.warnings)


def test_coin_account_cannot_be_a_default(world):
    conn, now = world
    acc.add_account(conn, name="Test Coins", description=None, unit="BTC", opening_amount_eur=None,
                    opening_units=None, opening_date=None, opening_time=None, default_for=None, price_ref=None,
                    tz=AMS, now=now)
    with pytest.raises(UserError) as e:
        acc.update_account(conn, "Test Coins", default_for="expense")
    assert e.value.code == "invalid_default"
