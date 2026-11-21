"""The MCP server: registers tools, adds /health and the token check, starts HTTP.

Tools here are thin: read input → call a service → return a typed answer.
Run locally:  uv run --env-file .env python -m app.server
"""

import functools
import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from app import db
from app.auth import BearerAuthMiddleware
from app.config import Settings, load_settings
from app.errors import UserError
from app.instructions import SERVER_INSTRUCTIONS
from app.models import (AddAccountOut, ContextOut, FindOut, PreviewOut, SaveOut, TransactionIn, TxType,
                        VoidOut)
from app.services import accounts as accounts_service
from app.services import context as context_service
from app.services import transactions as tx_service

log = logging.getLogger("finance_agent")

mcp = MCPServer(name="finance-agent", instructions=SERVER_INSTRUCTIONS)

_settings: Settings | None = None


def settings() -> Settings:
    """Loaded once, on first use (in tests: from the test's environment)."""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def logged_tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wraps a tool: logs name, duration and result code (never inputs or outputs, they're private),
    turns UserError into a helpful message for Claude, and hides internal details behind a reference id."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        ref = uuid.uuid4().hex[:8]
        outcome = "ok"
        try:
            return fn(*args, **kwargs)
        except UserError as e:
            outcome = e.code
            raise ToolError(e.for_model()) from e
        except Exception as e:
            outcome = "internal_error"
            log.exception("tool=%s ref=%s internal error", fn.__name__, ref)
            raise ToolError(f"internal error (ref {ref}). Tell the user something went wrong; "
                            "do not retry more than once.") from e
        finally:
            ms = (time.perf_counter() - start) * 1000
            log.info("tool=%s ms=%.0f result=%s ref=%s", fn.__name__, ms, outcome, ref)

    return wrapper


def _now() -> datetime:
    return datetime.now(settings().timezone)


@mcp.tool(annotations=ToolAnnotations(title="Get context", read_only_hint=True))
@logged_tool
def get_context() -> ContextOut:
    """Call this first in every chat. Returns today's date and weekday in the user's time zone,
    the accounts (with descriptions, defaults and balances) and the active categories (with kind,
    parent, description and aliases)."""
    with db.tx() as conn:
        return context_service.get_context(conn, settings().timezone)


@mcp.tool(annotations=ToolAnnotations(title="Add account"))
@logged_tool
def add_account(
    name: str,
    description: str | None = None,
    unit: str = "EUR",
    opening_amount_eur: Decimal | None = None,
    opening_units: Decimal | None = None,
    opening_date: str | None = None,
    opening_time: str | None = None,
    default_for: Literal["expense", "income", "both"] | None = None,
    price_ref: str | None = None,
) -> AddAccountOut:
    """Create an account (day-one setup or later). description = one line on what it's for (Claude reads it
    every chat). unit = EUR, or a coin code like BTC for a coin account. The opening balance is recorded at
    this moment unless opening_date/opening_time are given; for a coin account give opening_units (coins)
    and opening_amount_eur (what was paid, may be 0). default_for marks the default account for daily
    expenses, income or both. Confirm the details with the user before calling."""
    with db.tx() as conn:
        return accounts_service.add_account(
            conn, name=name, description=description, unit=unit, opening_amount_eur=opening_amount_eur,
            opening_units=opening_units, opening_date=opening_date, opening_time=opening_time,
            default_for=default_for, price_ref=price_ref, tz=settings().timezone, now=_now())


@mcp.tool(annotations=ToolAnnotations(title="Preview transactions"))
@logged_tool
def preview_transactions(transactions: list[TransactionIn], replaces_draft_id: str | None = None) -> PreviewOut:
    """Check one or more money events and prepare them for saving. Saves NOTHING except a draft.
    Returns status needs_info with exact questions (ask them, then preview again), or status ready with a
    draft_id, one summary line per transaction and warnings: show all of them to the user and ask for a
    clear yes. Types: expense, income, refund, transfer (account → to_account), opening, revaluation
    (give new_value_eur). Dates as YYYY-MM-DD (+ optional HH:MM) in the user's time zone; omit for now.
    If the user corrects something, preview again with replaces_draft_id."""
    with db.tx() as conn:
        return tx_service.preview_transactions(conn, transactions, settings().timezone, _now(), replaces_draft_id)


@mcp.tool(annotations=ToolAnnotations(title="Save draft", idempotent_hint=True))
@logged_tool
def save_draft(draft_id: str) -> SaveOut:
    """Save a previewed draft — only after the user clearly said yes to its summary. Safe to repeat:
    a second call returns status already_saved with the same transaction ids (nothing is saved twice).
    Drafts expire after 30 minutes."""
    with db.tx() as conn:
        return tx_service.save_draft(conn, draft_id)


@mcp.tool(annotations=ToolAnnotations(title="Find transactions", read_only_hint=True))
@logged_tool
def find_transactions(
    text: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    account: str | None = None,
    category: str | None = None,
    type: TxType | None = None,
    include_void: bool = False,
    limit: int = 20,
) -> FindOut:
    """Look up recorded transactions (newest first) to answer "what did I buy yesterday?" or to find the id
    of one to fix or void. text searches merchant, note and receipt items. A parent category also finds its
    children. Dates as YYYY-MM-DD. Names must match exactly or by alias."""
    with db.tx() as conn:
        return tx_service.find_transactions(conn, text=text, date_from=date_from, date_to=date_to,
                                            account=account, category=category, type=type,
                                            include_void=include_void, limit=limit)


@mcp.tool(annotations=ToolAnnotations(title="Void or restore a transaction", destructive_hint=True))
@logged_tool
def void_transaction(transaction_id: int, reason: str = "", undo: bool = False,
                     confirm_opening: bool = False) -> VoidOut:
    """Cancel a transaction (it is kept, marked void, and no longer counts), or restore it with undo=true.
    Nothing is ever deleted; every void and undo is recorded in the history. Confirm with the user first
    (use find_transactions to show which one). Voiding an opening balance needs confirm_opening=true."""
    with db.tx() as conn:
        return tx_service.void_transaction(conn, transaction_id, reason, undo, confirm_opening)


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """For the hosting service: 'the server is alive'. No auth, no database, no data."""
    return JSONResponse({"status": "ok"})


def create_app(config: Settings | None = None) -> ASGIApp:
    """Builds the web app: MCP at /mcp, /health, and the token check in front of everything."""
    global _settings
    config = config or settings()
    _settings = config
    security = None  # local default: only 127.0.0.1 / localhost host names are accepted
    if config.allowed_hosts:
        security = TransportSecuritySettings(allowed_hosts=list(config.allowed_hosts))
    app = mcp.streamable_http_app(stateless_http=True, json_response=True, transport_security=security)
    return BearerAuthMiddleware(app, config.tokens)


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(create_app(), host="127.0.0.1", port=8000, access_log=False)
