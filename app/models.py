"""Shapes of what tools accept and return (Pydantic). Shared later by our own agent (stage C) and any dashboard.

Money is Decimal with at most 2 decimals (never float); in JSON it travels as an exact string.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

TxType = Literal["expense", "income", "refund", "transfer", "opening", "revaluation"]
QuantityUnit = Literal["pcs", "kg", "g", "l", "ml"]


def Money(description: str = "", **kw) -> Decimal:  # noqa: N802 — reads like a type in the models below
    return Field(max_digits=12, decimal_places=2, description=description, **kw)


# --- inputs -------------------------------------------------------------------------

class ItemIn(BaseModel):
    name_raw: str = Field(min_length=1, max_length=200, description="As printed on the receipt")
    name_clean: str | None = Field(None, max_length=200, description="Readable name; reuse the user's earlier names")
    item_type: str | None = Field(None, max_length=100, description="Short product type, e.g. 'ice cream', 'milk'")
    quantity: Decimal = Field(Decimal(1), gt=0)
    quantity_unit: QuantityUnit | None = Field(None, description="pcs, kg, g, l or ml")
    price_eur: Decimal = Money("Line total after discount; discount and deposit-return lines are negative")
    discount_eur: Decimal | None = Money("Information only; already included in price_eur", default=None)


class TransactionIn(BaseModel):
    type: TxType = "expense"
    amount_eur: Decimal | None = Money(
        "Euros, positive. Not needed for revaluation (give new_value_eur). Opening may be any amount.",
        default=None)
    date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$",
                             description="YYYY-MM-DD in the user's time zone; omit for 'now'")
    time: str | None = Field(None, pattern=r"^\d{2}:\d{2}$", description="HH:MM; a date without time = 12:00")
    account: str | None = Field(None, max_length=100,
                                description="Account name or alias; omit to use the default account")
    to_account: str | None = Field(None, max_length=100, description="Only for transfers: the receiving account")
    category: str | None = Field(None, max_length=100,
                                 description="Required for expense and refund; optional for income and transfer")
    merchant: str | None = Field(None, max_length=200)
    note: str | None = Field(None, max_length=1000)
    tags: list[str] = Field(default_factory=list, max_length=10)
    units: Decimal | None = Field(None, gt=0, description="Coins (for coin accounts) or foreign money paid")
    unit: str | None = Field(None, pattern=r"^[A-Za-z0-9]{2,10}$", description="e.g. BTC, USD")
    new_value_eur: Decimal | None = Money("Revaluation only: the value the user says the account is worth now",
                                          default=None)
    items: list[ItemIn] = Field(default_factory=list, max_length=200)
    source: Literal["chat", "receipt"] = "chat"


# --- outputs ------------------------------------------------------------------------

class CategoryOut(BaseModel):
    id: int
    name: str
    kind: str = Field(description="regular = everyday (in the forecast); structural = counts as spending, "
                                  "not in the forecast; excluded = neither spending nor income (e.g. loans)")
    parent: str | None = None
    description: str | None = None
    aliases: list[str] = Field(default_factory=list)


class AccountOut(BaseModel):
    id: int
    name: str
    description: str | None = None
    unit: str = "EUR"
    is_default_expense: bool = False
    is_default_income: bool = False
    is_active: bool = True
    balance_eur: Decimal | None = Field(None, description="EUR accounts: current balance")
    balance_units: Decimal | None = Field(None, description="Coin accounts: number of coins held")
    net_invested_eur: Decimal | None = Field(None, description="Coin accounts: euros put in minus taken out")


class ContextOut(BaseModel):
    today: date = Field(description="Today's date in the user's time zone. Use it for 'today', 'yesterday', etc.")
    weekday: str
    timezone: str
    accounts: list[AccountOut]
    categories: list[CategoryOut]
    notes: list[str] = Field(default_factory=list, description="Anything Claude should know before acting")


class AddAccountOut(BaseModel):
    account: AccountOut
    opening_transaction_id: int | None = None
    warnings: list[str] = Field(default_factory=list)


class TxPreview(BaseModel):
    number: int
    summary: str = Field(description="Show this line to the user before asking for 'yes'")
    type: TxType
    amount_eur: Decimal
    account: str
    to_account: str | None = None
    category: str | None = None
    occurred_at: datetime
    items_count: int = 0


class PreviewOut(BaseModel):
    status: Literal["ready", "needs_info"]
    draft_id: str | None = None
    expires_at: datetime | None = None
    transactions: list[TxPreview] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list, description="Ask the user exactly these, then preview again")
    next_step: str


class ItemOut(BaseModel):
    name_raw: str
    name_clean: str | None = None
    item_type: str | None = None
    quantity: Decimal
    quantity_unit: str | None = None
    price_eur: Decimal


class TransactionOut(BaseModel):
    id: int
    occurred_at: datetime
    day: date
    type: TxType
    amount_eur: Decimal
    account: str
    to_account: str | None = None
    category: str | None = None
    merchant: str | None = None
    note: str | None = None
    tags: list[str] = Field(default_factory=list)
    units: Decimal | None = None
    unit: str | None = None
    status: Literal["active", "void"]
    void_reason: str | None = None
    items: list[ItemOut] = Field(default_factory=list)


class SaveOut(BaseModel):
    status: Literal["saved", "already_saved"]
    transaction_ids: list[int]
    warnings: list[str] = Field(default_factory=list)


class FindOut(BaseModel):
    transactions: list[TransactionOut]
    truncated: bool = Field(description="True if there were more matches than the limit")


class VoidOut(BaseModel):
    status: Literal["voided", "restored"]
    transaction: TransactionOut
    warnings: list[str] = Field(default_factory=list)
