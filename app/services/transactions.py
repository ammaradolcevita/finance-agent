"""Recording money safely: preview → draft → save; finding records; void and undo.

Rules in plain words:
- The preview never guesses. If something is missing or unclear, it returns questions and saves nothing.
- A draft is a frozen copy of what will be saved. Saving takes only the draft id, so nothing can change
  between what the user saw and what is stored.
- Saving is single-use and repeatable safely: a second save returns "already saved" with the same ids.
- Nothing is ever deleted: a mistake is voided (and can be restored); every void and undo goes to the change log.
Services never commit: the tool layer runs one database transaction per tool call.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg import errors
from psycopg.types.json import Jsonb

from app.errors import UserError
from app.models import (FindOut, ItemOut, PreviewOut, SaveOut, TransactionIn, TransactionOut, TxPreview,
                        VoidOut)
from app.services.accounts import AccountRow, active_accounts, balance_at, opening_moment
from app.services.dates import check_when, in_words, resolve_when
from app.services.resolve import Candidate, resolve

DRAFT_LIFETIME = timedelta(minutes=30)
RECEIPT_TOLERANCE = Decimal("0.01")
MAX_FIND = 100
LABELS = {"expense": "Expense", "income": "Income", "refund": "Refund", "transfer": "Transfer",
          "opening": "Opening balance", "revaluation": "Revaluation"}


@dataclass(frozen=True)
class CategoryRow:
    id: int
    name: str
    kind: str
    parent_id: int | None
    aliases: tuple[str, ...]

    def candidate(self) -> Candidate:
        return Candidate(self.id, self.name, self.aliases)


def active_categories(conn: psycopg.Connection) -> list[CategoryRow]:
    rows = conn.execute(
        "SELECT id, name, kind, parent_id, aliases FROM categories WHERE is_active ORDER BY lower(name)").fetchall()
    return [CategoryRow(r[0], r[1], r[2], r[3], tuple(r[4] or ())) for r in rows]


def _check_cents(value: Decimal | None, what: str) -> None:
    if value is not None and value.as_tuple().exponent < -2:
        raise UserError("invalid_amount", f"{what} can have at most 2 decimals (cents).")


# --- preview --------------------------------------------------------------------------

class _Notes:
    """Collects questions (block saving) and warnings (shown to the user) for one preview."""

    def __init__(self, prefix: str):
        self.prefix = prefix
        self.questions: list[str] = []
        self.warnings: list[str] = []

    def ask(self, text: str) -> None:
        self.questions.append(self.prefix + text)

    def warn(self, text: str) -> None:
        self.warnings.append(self.prefix + text)


def _pick_account(text: str, accounts: list[AccountRow], label: str, notes: _Notes) -> AccountRow | None:
    m = resolve(text, [a.candidate() for a in accounts])
    if m.kind == "none":
        names = ", ".join(a.name for a in accounts) or "none yet"
        hint = f" Did you mean: {', '.join(m.suggestions)}?" if m.suggestions else ""
        notes.ask(f'Which {label}? "{text}" doesn\'t match an account.{hint} Existing accounts: {names}.')
        return None
    if m.kind == "fuzzy":
        notes.warn(f'Interpreted {label} "{text}" as "{m.candidate.name}".')
    return next(a for a in accounts if a.id == m.candidate.id)


def _pick_category(text: str, categories: list[CategoryRow], notes: _Notes) -> CategoryRow | None:
    m = resolve(text, [c.candidate() for c in categories])
    if m.kind == "none":
        hint = f" Did you mean: {', '.join(m.suggestions)}?" if m.suggestions else ""
        notes.ask(f'Unknown category "{text}".{hint} Pick an existing category, or propose a new one to the user.')
        return None
    if m.kind == "fuzzy":
        notes.warn(f'Interpreted category "{text}" as "{m.candidate.name}".')
    return next(c for c in categories if c.id == m.candidate.id)


def _later_revaluation_warning(conn: psycopg.Connection, account_ids: list[int], when: datetime) -> str | None:
    row = conn.execute(
        "SELECT a.name, t.occurred_at FROM transactions t JOIN accounts a ON a.id = t.account_id "
        "WHERE t.type = 'revaluation' AND t.status = 'active' AND t.account_id = ANY(%s) AND t.occurred_at > %s "
        "ORDER BY t.occurred_at LIMIT 1",
        (account_ids, when)).fetchone()
    if not row:
        return None
    return (f"{row[0]} has a later revaluation ({row[1]:%d %b %Y}) that was based on the balance at that time; "
            "this change may make it no longer match the value the user stated. Offer to check it.")


def _normalize(conn: psycopg.Connection, t: TransactionIn, accounts: list[AccountRow],
               categories: list[CategoryRow], tz: ZoneInfo, now: datetime, notes: _Notes) -> dict | None:
    """Checks one transaction. Returns the frozen draft payload, or None if questions were asked."""
    asked_before = len(notes.questions)
    if t.to_account and t.type != "transfer":
        raise UserError("invalid_to_account", "to_account is only for transfers.")
    if t.category and t.type in ("opening", "revaluation"):
        raise UserError("invalid_category", f"A {t.type} has no category.")
    _check_cents(t.amount_eur, "The amount")
    _check_cents(t.new_value_eur, "The new value")

    # 1. Accounts (the default account is used when none is named).
    account = None
    if t.account:
        account = _pick_account(t.account, accounts, "account", notes)
    elif t.type in ("expense", "refund", "income"):
        flag = "is_default_income" if t.type == "income" else "is_default_expense"
        account = next((a for a in accounts if getattr(a, flag)), None)
        if account is None:
            names = ", ".join(a.name for a in accounts)
            notes.ask(f"Which account is this for? (No default account is set.) Existing accounts: {names}.")
    else:
        notes.ask(f"Which account is this {t.type} for?")
    to_account = None
    if t.type == "transfer":
        if not t.to_account:
            notes.ask("Which account does the money go to?")
        else:
            to_account = _pick_account(t.to_account, accounts, "receiving account", notes)
        if account and to_account and account.id == to_account.id:
            notes.ask("The transfer goes from and to the same account. Which two accounts?")

    # 2. Category (required for spending and refunds).
    category = None
    if t.category:
        category = _pick_category(t.category, categories, notes)
    elif t.type in ("expense", "refund"):
        notes.ask("Which category? Use one of the categories from get_context.")

    # 3. When (in the user's time zone; the future is not allowed).
    when, exact = resolve_when(t.date, t.time, tz, now)
    error, old_warning = check_when(when, now)
    if error:
        raise UserError("invalid_date", error)

    # 4. Amount (a revaluation is calculated from the value the user states).
    amount, stated, balance_then = t.amount_eur, None, None
    if t.type == "revaluation":
        if account and account.is_coin:
            raise UserError("invalid_type", "Coin accounts are valued as coins × price; no revaluation is needed.")
        if t.new_value_eur is None:
            notes.ask("What is the account worth now, in euros?")
        elif account:
            balance_then, _ = balance_at(conn, account.id)
            amount = t.new_value_eur - balance_then
            if amount == 0:
                raise UserError("no_change", f"{account.name} is already at {balance_then:.2f} €; nothing to record.")
            stated = t.new_value_eur
    elif amount is None:
        notes.ask("How much was it, in euros?")
    elif t.type != "opening" and amount <= 0:
        raise UserError("invalid_amount",
                        "Amounts are positive; the type gives the direction (use a refund for money back).")

    if len(notes.questions) > asked_before:
        return None

    # 5. Coins and foreign money.
    units, unit = t.units, (t.unit.upper() if t.unit else None)
    coin_accounts = [a for a in (account, to_account) if a and a.is_coin]
    if coin_accounts:
        if t.type in ("expense", "income", "refund"):
            raise UserError("invalid_type", "Coin accounts only take transfers (buying or selling) and an opening.")
        coins = {a.unit for a in coin_accounts}
        if len(coins) > 1:
            raise UserError("invalid_transfer",
                            "Swapping one coin for another: record it as two transfers through a euro account.")
        coin = coins.pop()
        if unit and unit != coin:
            raise UserError("invalid_unit", f"This account counts {coin}, not {unit}.")
        if units is None:
            notes.ask(f"How many {coin}?")
            return None
        unit = coin
        if t.type == "transfer" and account.is_coin:
            _, held = balance_at(conn, account.id)
            if units > held:
                raise UserError("not_enough_coins", f"{account.name} holds {held} {coin}; it can't send {units}.")
    elif units is not None and (unit is None or unit == "EUR"):
        raise UserError("invalid_unit", "For a foreign amount give the currency code too, e.g. unit=USD.")
    elif unit and units is None:
        unit = None

    # 6. Opening rules, dates before the opening, later revaluations.
    if t.type == "opening":
        if opening_moment(conn, account.id):
            raise UserError("opening_exists", f"{account.name} already has an opening balance. "
                                              "Fix it with update_transaction or void it first.")
    else:
        start = opening_moment(conn, account.id)
        if start and when < start:
            notes.warn(f"This is dated before {account.name}'s opening balance ({start:%d %b %Y}); "
                       "it counts in statistics but not in the balance.")
    later = _later_revaluation_warning(conn, [a.id for a in (account, to_account) if a], when)
    if later and t.type != "revaluation":
        notes.warn(later)
    if t.type == "income" and conn.execute(
            "SELECT 1 FROM transactions WHERE account_id = %s AND type = 'revaluation' AND status = 'active' "
            "LIMIT 1", (account.id,)).fetchone():
        notes.warn(f"{account.name} is valued by revaluations: report its new value instead of recording "
                   "profit as income, otherwise the profit counts twice.")

    # 7. Possible duplicate (same account, type, amount, category and local day).
    if t.type in ("expense", "income", "refund"):
        dup = conn.execute(
            "SELECT id FROM transactions WHERE status = 'active' AND account_id = %s AND type = %s "
            "AND amount_eur = %s AND category_id IS NOT DISTINCT FROM %s AND occurred_at::date = %s::date LIMIT 1",
            (account.id, t.type, amount, category.id if category else None, when)).fetchone()
        if dup:
            notes.warn(f"Possible duplicate of transaction #{dup[0]} (same amount, category and day).")

    # 8. Receipt: do the lines add up to the total?
    if t.items and t.type in ("expense", "refund"):
        total = sum((i.price_eur for i in t.items), Decimal("0"))
        if abs(total - amount) > RECEIPT_TOLERANCE:
            notes.warn(f"The items add up to {total:.2f} €, but the total is {amount:.2f} € "
                       f"(difference {total - amount:+.2f} €). Please check the receipt.")
    if old_warning:
        notes.warn(old_warning)

    return {
        "type": t.type,
        "amount_eur": str(amount),
        "occurred_at": when.isoformat(),
        "exact_time": exact,
        "account_id": account.id, "account_name": account.name,
        "to_account_id": to_account.id if to_account else None,
        "to_account_name": to_account.name if to_account else None,
        "category_id": category.id if category else None,
        "category_name": category.name if category else None,
        "merchant": t.merchant, "note": t.note,
        "tags": sorted({tag.strip().lower() for tag in t.tags if tag.strip()}),
        "units": str(units) if units is not None else None, "unit": unit,
        "stated_balance_eur": str(stated) if stated is not None else None,
        "balance_at_preview": str(balance_then) if balance_then is not None else None,
        "source": t.source,
        "items": [i.model_dump(mode="json") for i in t.items],
    }


def _summary(p: dict, tz: ZoneInfo) -> str:
    amount = Decimal(p["amount_eur"])
    head = f"{LABELS[p['type']]} {amount:+.2f} €" if p["type"] == "revaluation" else f"{LABELS[p['type']]} {amount:.2f} €"
    if p["units"]:
        head += f" ({p['units']} {p['unit']})"
    parts = [head, p["account_name"] + (f" → {p['to_account_name']}" if p["to_account_name"] else "")]
    if p["category_name"]:
        parts.append(p["category_name"])
    parts.append(in_words(datetime.fromisoformat(p["occurred_at"]), tz, p["exact_time"]))
    if p["stated_balance_eur"]:
        parts.append(f"new value {Decimal(p['stated_balance_eur']):.2f} € (was {Decimal(p['balance_at_preview']):.2f} €)")
    if p["merchant"]:
        parts.append(p["merchant"])
    if p["note"]:
        parts.append(f'"{p["note"]}"')
    if p["tags"]:
        parts.append(" ".join("#" + tag for tag in p["tags"]))
    if p["items"]:
        parts.append(f"{len(p['items'])} items")
    return " · ".join(parts)


def preview_transactions(conn: psycopg.Connection, transactions: list[TransactionIn], tz: ZoneInfo,
                         now: datetime, replaces_draft_id: str | None = None) -> PreviewOut:
    if not transactions:
        raise UserError("empty", "Give at least one transaction.")
    accounts = active_accounts(conn)
    if not accounts:
        raise UserError("no_accounts", "No accounts exist yet. Do the day-one setup first (add_account).")
    categories = active_categories(conn)

    payloads, questions, warnings = [], [], []
    for n, t in enumerate(transactions, start=1):
        notes = _Notes(f"#{n}: " if len(transactions) > 1 else "")
        payload = _normalize(conn, t, accounts, categories, tz, now, notes)
        questions += notes.questions
        warnings += notes.warnings
        if payload:
            payloads.append(payload)

    if questions:
        return PreviewOut(status="needs_info", questions=questions, warnings=warnings,
                          next_step="Ask the user exactly these questions, then call preview_transactions again "
                                    "with the answers. Nothing was saved.")

    if replaces_draft_id:
        _valid_uuid(replaces_draft_id)
        conn.execute("UPDATE drafts SET cancelled_at = now() WHERE id = %s AND saved_at IS NULL",
                     (replaces_draft_id,))
    draft_id = str(uuid.uuid4())
    conn.execute("INSERT INTO drafts (id, payload) VALUES (%s, %s)",
                 (draft_id, Jsonb({"transactions": payloads})))
    previews = [
        TxPreview(number=n, summary=_summary(p, tz), type=p["type"], amount_eur=Decimal(p["amount_eur"]),
                  account=p["account_name"], to_account=p["to_account_name"], category=p["category_name"],
                  occurred_at=datetime.fromisoformat(p["occurred_at"]), items_count=len(p["items"]))
        for n, p in enumerate(payloads, start=1)
    ]
    return PreviewOut(status="ready", draft_id=draft_id, expires_at=now + DRAFT_LIFETIME,
                      transactions=previews, warnings=warnings,
                      next_step="Show the user every summary line and warning. Only if they clearly say yes, "
                                "call save_draft with this draft_id. If they correct something, preview again "
                                "with replaces_draft_id.")


# --- save ----------------------------------------------------------------------------

def _valid_uuid(value: str) -> None:
    try:
        uuid.UUID(value)
    except ValueError as e:
        raise UserError("draft_not_found", "That draft id doesn't exist. Preview again.") from e


def save_draft(conn: psycopg.Connection, draft_id: str) -> SaveOut:
    _valid_uuid(draft_id)
    # FOR UPDATE locks the draft row: two saves at the same moment wait for each other, so only one inserts.
    row = conn.execute(
        "SELECT payload, saved_ids, saved_at IS NOT NULL, cancelled_at IS NOT NULL,"
        "       created_at < now() - interval '30 minutes' "
        "FROM drafts WHERE id = %s FOR UPDATE", (draft_id,)).fetchone()
    if not row:
        raise UserError("draft_not_found", "That draft id doesn't exist. Preview again.")
    payload, saved_ids, saved, cancelled, expired = row
    if saved:
        return SaveOut(status="already_saved", transaction_ids=list(saved_ids or []),
                       warnings=["This draft was already saved earlier; nothing was saved twice."])
    if cancelled:
        raise UserError("draft_replaced", "This draft was replaced by a newer preview. Save the newer one.")
    if expired:
        raise UserError("draft_expired", "This preview is older than 30 minutes. Preview again and ask the "
                                         "user to confirm.")

    items: list[dict] = payload["transactions"]
    account_ids = {p["account_id"] for p in items} | {p["to_account_id"] for p in items if p["to_account_id"]}
    category_ids = {p["category_id"] for p in items if p["category_id"]}
    active_acc = {r[0] for r in conn.execute(
        "SELECT id FROM accounts WHERE id = ANY(%s) AND is_active", (list(account_ids),))}
    active_cat = {r[0] for r in conn.execute(
        "SELECT id FROM categories WHERE id = ANY(%s) AND is_active", (list(category_ids),))}
    if account_ids - active_acc or category_ids - active_cat:
        raise UserError("stale_draft", "An account or category in this draft was archived meanwhile. Preview again.")

    ids: list[int] = []
    for p in items:
        if p["type"] == "revaluation":
            now_balance, _ = balance_at(conn, p["account_id"])
            if now_balance != Decimal(p["balance_at_preview"]):
                raise UserError("balance_changed",
                                f"{p['account_name']}'s balance changed since the preview "
                                f"({Decimal(p['balance_at_preview']):.2f} € → {now_balance:.2f} €). "
                                "Preview the revaluation again.")
        try:
            tid = conn.execute(
                "INSERT INTO transactions (occurred_at, type, amount_eur, account_id, to_account_id, category_id,"
                " merchant, note, tags, units, unit, stated_balance_eur, draft_id, source) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (p["occurred_at"], p["type"], p["amount_eur"], p["account_id"], p["to_account_id"],
                 p["category_id"], p["merchant"], p["note"], p["tags"], p["units"], p["unit"],
                 p["stated_balance_eur"], draft_id, p["source"])).fetchone()[0]
        except errors.UniqueViolation as e:
            raise UserError("opening_exists", f"{p['account_name']} already has an opening balance.") from e
        for i in p["items"]:
            conn.execute(
                "INSERT INTO items (transaction_id, name_raw, name_clean, item_type, quantity, quantity_unit,"
                " price_eur, discount_eur) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (tid, i["name_raw"], i["name_clean"], i["item_type"], i["quantity"], i["quantity_unit"],
                 i["price_eur"], i["discount_eur"] or 0))
        ids.append(tid)

    conn.execute("UPDATE drafts SET saved_at = now(), saved_ids = %s WHERE id = %s", (ids, draft_id))
    return SaveOut(status="saved", transaction_ids=ids)


# --- reading -----------------------------------------------------------------------

_SELECT = (
    "SELECT t.id, t.occurred_at, t.occurred_at::date, t.type, t.amount_eur, a.name, ta.name, c.name,"
    "       t.merchant, t.note, t.tags, t.units, t.unit, t.status, t.void_reason "
    "FROM transactions t JOIN accounts a ON a.id = t.account_id "
    "LEFT JOIN accounts ta ON ta.id = t.to_account_id LEFT JOIN categories c ON c.id = t.category_id "
)
_FIELDS = ["id", "occurred_at", "day", "type", "amount_eur", "account", "to_account", "category",
           "merchant", "note", "tags", "units", "unit", "status", "void_reason"]


def load_transactions(conn: psycopg.Connection, ids: list[int]) -> list[TransactionOut]:
    if not ids:
        return []
    rows = conn.execute(_SELECT + "WHERE t.id = ANY(%s) ORDER BY t.occurred_at DESC, t.id DESC", (ids,)).fetchall()
    items: dict[int, list[ItemOut]] = {}
    for r in conn.execute(
            "SELECT transaction_id, name_raw, name_clean, item_type, quantity, quantity_unit, price_eur "
            "FROM items WHERE transaction_id = ANY(%s) ORDER BY id", (ids,)):
        items.setdefault(r[0], []).append(ItemOut(name_raw=r[1], name_clean=r[2], item_type=r[3],
                                                  quantity=r[4], quantity_unit=r[5], price_eur=r[6]))
    result = []
    for r in rows:
        data: dict[str, Any] = dict(zip(_FIELDS, r))
        data["tags"] = list(data["tags"] or [])
        result.append(TransactionOut(**data, items=items.get(data["id"], [])))
    return result


def _exact(conn: psycopg.Connection, table: str, text: str) -> int:
    """For filters: an exact name or alias only (no guessing in searches)."""
    if table == "accounts":
        rows = conn.execute("SELECT id, name FROM accounts").fetchall()
        candidates = [Candidate(r[0], r[1]) for r in rows]
    else:
        rows = conn.execute("SELECT id, name, aliases FROM categories").fetchall()
        candidates = [Candidate(r[0], r[1], tuple(r[2] or ())) for r in rows]
    m = resolve(text, candidates, allow_fuzzy=False)
    if m.kind == "none":
        hint = f" Did you mean: {', '.join(m.suggestions)}?" if m.suggestions else ""
        raise UserError("unknown_name", f'No {table[:-1].replace("ie", "y")} called "{text}".{hint}')
    return m.candidate.id


def _parse_day(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise UserError("invalid_date", f"{label} must be YYYY-MM-DD.") from e


def find_transactions(conn: psycopg.Connection, *, text: str | None = None, date_from: str | None = None,
                      date_to: str | None = None, account: str | None = None, category: str | None = None,
                      type: str | None = None, include_void: bool = False, limit: int = 20) -> FindOut:
    clauses: list[str] = []
    params: list[Any] = []
    if not include_void:
        clauses.append("t.status = 'active'")
    if text:
        like = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clauses.append("(t.merchant ILIKE %s OR t.note ILIKE %s OR EXISTS (SELECT 1 FROM items i "
                       "WHERE i.transaction_id = t.id AND (i.name_raw ILIKE %s OR i.name_clean ILIKE %s)))")
        params += [like] * 4
    if date_from:
        clauses.append("t.occurred_at::date >= %s")
        params.append(_parse_day(date_from, "date_from"))
    if date_to:
        clauses.append("t.occurred_at::date <= %s")
        params.append(_parse_day(date_to, "date_to"))
    if account:
        account_id = _exact(conn, "accounts", account)
        clauses.append("(t.account_id = %s OR t.to_account_id = %s)")
        params += [account_id, account_id]
    if category:
        category_id = _exact(conn, "categories", category)
        clauses.append("(t.category_id = %s OR t.category_id IN (SELECT id FROM categories WHERE parent_id = %s))")
        params += [category_id, category_id]
    if type:
        clauses.append("t.type = %s")
        params.append(type)
    limit = max(1, min(limit, MAX_FIND))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""   # fixed SQL pieces only; data goes in params
    ids = [r[0] for r in conn.execute(
        "SELECT t.id FROM transactions t" + where + " ORDER BY t.occurred_at DESC, t.id DESC LIMIT %s",
        (*params, limit + 1))]
    return FindOut(transactions=load_transactions(conn, ids[:limit]), truncated=len(ids) > limit)


# --- void and undo --------------------------------------------------------------------

def _log(conn: psycopg.Connection, transaction_id: int, action: str, old: dict, new: dict) -> None:
    conn.execute("INSERT INTO change_log (entity, entity_id, action, old_values, new_values) "
                 "VALUES ('transaction', %s, %s, %s, %s)", (transaction_id, action, Jsonb(old), Jsonb(new)))


def void_transaction(conn: psycopg.Connection, transaction_id: int, reason: str = "", undo: bool = False,
                     confirm_opening: bool = False) -> VoidOut:
    row = conn.execute(
        "SELECT status, type, account_id, to_account_id, amount_eur, category_id, occurred_at, void_reason "
        "FROM transactions WHERE id = %s FOR UPDATE", (transaction_id,)).fetchone()
    if not row:
        raise UserError("not_found", f"There is no transaction #{transaction_id}.")
    status, type_, account_id, to_account_id, amount, category_id, when, old_reason = row
    warnings: list[str] = []

    if not undo:
        if status == "void":
            raise UserError("already_void", f"Transaction #{transaction_id} is already voided.")
        if not reason.strip():
            raise UserError("missing_reason", "Give a short reason for voiding (e.g. 'entered twice').")
        if type_ == "opening" and not confirm_opening:
            raise UserError("confirm_opening",
                            "This is the account's opening balance: voiding it changes the balance by its whole "
                            "amount. Ask the user; only if they agree, call again with confirm_opening=true.")
        conn.execute("UPDATE transactions SET status = 'void', void_reason = %s, updated_at = now() WHERE id = %s",
                     (reason.strip(), transaction_id))
        _log(conn, transaction_id, "void", {"status": "active"}, {"status": "void", "void_reason": reason.strip()})
        result = "voided"
    else:
        if status == "active":
            raise UserError("not_void", f"Transaction #{transaction_id} is not voided.")
        dup = conn.execute(
            "SELECT id FROM transactions WHERE status = 'active' AND id <> %s AND account_id = %s AND type = %s "
            "AND amount_eur = %s AND category_id IS NOT DISTINCT FROM %s AND occurred_at::date = %s::date LIMIT 1",
            (transaction_id, account_id, type_, amount, category_id, when)).fetchone()
        if dup:
            warnings.append(f"Possible duplicate: #{dup[0]} looks the same (maybe it was entered again after the "
                            "void). Check with the user.")
        try:
            conn.execute("UPDATE transactions SET status = 'active', void_reason = NULL, updated_at = now() "
                         "WHERE id = %s", (transaction_id,))
        except errors.UniqueViolation as e:
            raise UserError("opening_exists", "That account already has another active opening balance.") from e
        _log(conn, transaction_id, "undo_void", {"status": "void", "void_reason": old_reason}, {"status": "active"})
        result = "restored"

    later = _later_revaluation_warning(conn, [a for a in (account_id, to_account_id) if a], when)
    if later:
        warnings.append(later)
    return VoidOut(status=result, transaction=load_transactions(conn, [transaction_id])[0], warnings=warnings)
