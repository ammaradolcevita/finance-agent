"""Step 6 on the practice branch (rolled back): forecast baseline and balances on a date.
All names are made up. Skipped unless run with: uv run --env-file .env pytest"""

import calendar
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from app.errors import UserError
from app.models import TransactionIn as T
from app.services import accounts as acc
from app.services.forecast import forecast_month
from tests.helpers import AMS, record


def days_left(today: date) -> int:
    return calendar.monthrange(today.year, today.month)[1] - today.day


def test_baseline_uses_regular_spending_of_the_spending_account_only(world):
    conn, now = world
    record(conn, now, T(amount_eur=D("10"), category="Test Groceries Alpha"))                       # regular, card
    record(conn, now, T(amount_eur=D("450"), category="Test Rent Omega"))                           # structural
    record(conn, now, T(amount_eur=D("99"), category="Test Groceries Alpha", account="Test Main Bank"))  # other acc.
    f = forecast_month(conn, AMS, now)
    assert f.account == "Test Card"
    assert f.days_of_data == 1                         # the account was opened today (an hour ago)
    assert f.avg_per_day_eur == D("10.00")
    assert [c.category for c in f.by_category] == ["Test Groceries Alpha"]
    assert f.spent_this_month_eur == D("460") and f.spent_this_month_regular_eur == D("10")
    assert f.days_left == days_left(now.date())
    assert f.projected_rest_of_month_eur == D("10.00") * f.days_left
    assert f.balance_eur == D("150") - 10 - 450
    assert f.expected_end_balance_eur == f.balance_eur - f.projected_rest_of_month_eur
    assert any("rough" in n for n in f.notes)          # honest about having little data


def test_balance_given_by_the_user_is_used(world):
    conn, now = world
    f = forecast_month(conn, AMS, now, balance_eur=D("100"))
    assert f.balance_eur == D("100.00") and any("balance the user gave" in n for n in f.notes)


def test_no_spending_yet_projects_zero(world):
    conn, now = world
    f = forecast_month(conn, AMS, now)
    assert f.projected_rest_of_month_eur == D("0.00") and any("No regular spending" in n for n in f.notes)


def test_forecast_needs_a_euro_account(world):
    conn, now = world
    acc.add_account(conn, name="Test Coins", description=None, unit="BTC", opening_amount_eur=None,
                    opening_units=None, opening_date=None, opening_time=None, default_for=None, price_ref=None,
                    tz=AMS, now=now)
    with pytest.raises(UserError) as e:
        forecast_month(conn, AMS, now, account="Test Coins")
    assert e.value.code == "not_euro"


def test_balance_on_a_date_before_and_after_a_purchase(world):
    conn, now = world
    record(conn, now, T(amount_eur=D("20"), category="Test Groceries Alpha"))
    today = acc.balances_on(conn, AMS, now, account="Test Card")[0]
    assert today.balance_eur == D("130")
    yesterday = (now - timedelta(days=1)).date().isoformat()
    before = acc.balances_on(conn, AMS, now, account="Test Card", on=yesterday)[0]
    assert before.balance_eur == D("0") and "records start" in before.note   # opened only today


def test_all_accounts_and_no_future_dates(world):
    conn, now = world
    names = {b.account for b in acc.balances_on(conn, AMS, now)}
    assert {"Test Card", "Test Main Bank"} <= names
    with pytest.raises(UserError):
        acc.balances_on(conn, AMS, now, on=(now + timedelta(days=1)).date().isoformat())
