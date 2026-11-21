"""accounts_overview: "where did my money go?" — per account, for a period: start balance, money in by
source, money out by destination, end balance; plus flows for a chart and real income / spending totals.

Everything is calculated here in code from the ledger (v_movements). Two self-checks guard the numbers:
1. For every account: start + in − out must equal its balance at the end date, computed by a separate query.
2. If the period ends today: the end must equal the current balance from v_account_balances (a third path).
If a check fails, the answer says so instead of showing numbers that don't add up.
"""

from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg

from app.errors import UserError
from app.models import AccountPeriod, Flow, FlowLine, OverviewOut
from app.services.accounts import balance_at

ZERO = Decimal("0")


def _period(date_from: str | None, date_to: str | None, today: date) -> tuple[date, date]:
    try:
        start = date.fromisoformat(date_from) if date_from else today.replace(day=1)
        end = date.fromisoformat(date_to) if date_to else today
    except ValueError as e:
        raise UserError("invalid_date", "Dates must be YYYY-MM-DD.") from e
    if start > end:
        raise UserError("invalid_period", "date_from is after date_to.")
    return start, min(end, today)


def accounts_overview(conn: psycopg.Connection, tz: ZoneInfo, now: datetime, date_from: str | None = None,
                      date_to: str | None = None, account: str | None = None) -> OverviewOut:
    today = now.astimezone(tz).date()
    start, end = _period(date_from, date_to, today)

    accounts = {r[0]: r[1:] for r in conn.execute(
        "SELECT id, name, unit, description, is_active FROM accounts").fetchall()}
    if not accounts:
        raise UserError("no_accounts", "No accounts exist yet. Do the day-one setup first (add_account).")
    categories = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT c.id, c.name, p.name FROM categories c LEFT JOIN categories p ON p.id = c.parent_id").fetchall()}
    wanted = None
    if account:
        matches = [aid for aid, a in accounts.items() if a[0].casefold() == account.strip().casefold()]
        if not matches:
            raise UserError("unknown_account", f'No account called "{account}". Use the exact name.')
        wanted = matches[0]

    # Every ledger line up to the end of the period, from each account's opening on (earlier rows are
    # already inside the stated opening balance).
    lines = conn.execute(
        "WITH starts AS (SELECT account_id, occurred_at AS ls FROM transactions "
        "                WHERE type = 'opening' AND status = 'active') "
        "SELECT m.account_id, m.day, m.type, m.amount_eur, m.units, m.category_id, m.other_account_id "
        "FROM v_movements m LEFT JOIN starts s ON s.account_id = m.account_id "
        "WHERE m.day <= %s AND (s.ls IS NULL OR m.occurred_at >= s.ls)", (end,)).fetchall()

    start_eur: dict[int, Decimal] = defaultdict(lambda: ZERO)
    start_units: dict[int, Decimal] = defaultdict(lambda: ZERO)
    end_units: dict[int, Decimal] = defaultdict(lambda: ZERO)
    money_in: dict[int, dict[tuple, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    money_out: dict[int, dict[tuple, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    flows: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)

    for account_id, day, type_, amount, units, category_id, other_id in lines:
        amount = Decimal(amount)
        units = Decimal(units) if units is not None else ZERO
        end_units[account_id] += units
        if day < start:
            start_eur[account_id] += amount
            start_units[account_id] += units
            continue
        name = accounts[account_id][0]
        cat_name, cat_parent = categories.get(category_id, (None, None))
        if type_ == "transfer":
            other = accounts[other_id][0]
            if amount < 0:
                money_out[account_id][("transfer", other, None)] += -amount
                flows[(name, other)] += -amount          # counted once, on the sending side
            else:
                money_in[account_id][("transfer", other, None)] += amount
        elif type_ == "revaluation":
            side = money_in if amount > 0 else money_out
            side[account_id][("revaluation", "market", None)] += abs(amount)
            flows[("Market gain", name) if amount > 0 else (name, "Market loss")] += abs(amount)
        elif type_ == "opening":
            money_in[account_id][("opening", "opening balance", None)] += amount
            flows[("Opening balance", name)] += amount
        elif type_ in ("income", "refund"):
            label = cat_name or "other income"
            money_in[account_id][(type_, label, cat_parent)] += amount
            flows[(f"{'Income' if type_ == 'income' else 'Refund'}: {label}", name)] += amount
        else:  # expense
            label = cat_name or "uncategorized"
            money_out[account_id][("expense", label, cat_parent)] += -amount
            flows[(name, label)] += -amount

    result: list[AccountPeriod] = []
    warnings: list[str] = []
    checks: list[str] = []
    current = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT account_id, balance_eur, balance_units FROM v_account_balances").fetchall()}
    for account_id, (name, unit, description, is_active) in sorted(accounts.items(), key=lambda a: a[1][0].lower()):
        if wanted and account_id != wanted:
            continue
        ins = [FlowLine(kind=k[0], name=k[1], parent=k[2], amount_eur=v)
               for k, v in sorted(money_in[account_id].items(), key=lambda kv: -kv[1])]
        outs = [FlowLine(kind=k[0], name=k[1], parent=k[2], amount_eur=v)
                for k, v in sorted(money_out[account_id].items(), key=lambda kv: -kv[1])]
        if not is_active and not ins and not outs and start_eur[account_id] == 0:
            continue                                     # archived and untouched: leave it out
        total_in = sum((f.amount_eur for f in ins), ZERO)
        total_out = sum((f.amount_eur for f in outs), ZERO)
        begin = start_eur[account_id]
        finish = begin + total_in - total_out
        independent, _ = balance_at(conn, account_id, datetime.combine(end, time.max, tzinfo=tz))
        if finish != independent:
            warnings.append(f"Self-check failed for {name}: start + in − out = {finish:.2f} €, but the balance "
                            f"on {end} is {independent:.2f} €. Don't trust these numbers.")
        if end == today and unit == "EUR" and current.get(account_id, (None,))[0] is not None \
                and Decimal(current[account_id][0]) != finish:
            warnings.append(f"Self-check failed for {name}: the overview ends at {finish:.2f} € but the current "
                            f"balance is {Decimal(current[account_id][0]):.2f} €. Don't trust these numbers.")
        coin = unit != "EUR"
        result.append(AccountPeriod(
            account=name, unit=unit, description=description, start_eur=begin, money_in=ins, money_out=outs,
            total_in_eur=total_in, total_out_eur=total_out, end_eur=finish,
            start_units=start_units[account_id] if coin else None, end_units=end_units[account_id] if coin else None))

    if not warnings:
        checks.append("start + in − out = end for every account")
        if end == today:
            checks.append("end balances match the current balances")

    income = conn.execute("SELECT COALESCE(sum(amount_eur), 0) FROM v_income WHERE day BETWEEN %s AND %s",
                          (start, end)).fetchone()[0]
    spending = conn.execute("SELECT COALESCE(sum(amount_eur), 0) FROM v_spending WHERE day BETWEEN %s AND %s",
                            (start, end)).fetchone()[0]
    flow_list = [Flow(source=s, target=t, amount_eur=a) for (s, t), a in sorted(flows.items(), key=lambda kv: -kv[1])
                 if a != 0 and (not wanted or accounts[wanted][0] in (s, t))]
    return OverviewOut(date_from=start, date_to=end, accounts=result, flows=flow_list,
                       income_eur=Decimal(income), spending_eur=Decimal(spending), checks=checks, warnings=warnings)
