"""get_context and the tool list. The service is tested as a pure function; the tools through a real
in-process MCP client, with the database replaced by a stand-in (no database needed)."""

from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from mcp import Client

from app.models import AccountOut
from app.services.context import build_context

AMS = ZoneInfo("Europe/Amsterdam")
ROWS = [(1, "Coffee", "regular", None, None, []), (2, "Rent", "structural", None, "paying for housing", [])]


def test_today_uses_the_users_timezone_just_after_midnight():
    now_utc = datetime(2026, 11, 30, 23, 30, tzinfo=ZoneInfo("UTC"))   # = 00:30 on 1 Dec in Amsterdam
    ctx = build_context(ROWS, AMS, now=now_utc)
    assert str(ctx.today) == "2026-12-01"
    assert ctx.weekday == "Tuesday"
    assert ctx.timezone == "Europe/Amsterdam"


def test_categories_are_passed_through():
    ctx = build_context(ROWS, AMS, now=datetime(2026, 12, 1, 12, tzinfo=AMS))
    assert [(c.name, c.kind) for c in ctx.categories] == [("Coffee", "regular"), ("Rent", "structural")]
    assert ctx.categories[1].description == "paying for housing"


def test_without_accounts_claude_is_told_to_set_them_up():
    ctx = build_context(ROWS, AMS, now=datetime(2026, 12, 1, 12, tzinfo=AMS))
    assert ctx.accounts == []
    assert any("day-one setup" in n for n in ctx.notes)


def test_missing_defaults_are_pointed_out():
    accounts = [AccountOut(id=1, name="Main", is_default_income=True)]
    ctx = build_context(ROWS, AMS, now=datetime(2026, 12, 1, 12, tzinfo=AMS), accounts=accounts)
    assert any("daily expenses" in n for n in ctx.notes)
    assert not any("for income" in n for n in ctx.notes)


# --- through MCP -----------------------------------------------------------------

@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake_db(monkeypatch):
    import app.server as server
    import app.services.context as svc
    from app.config import load_settings

    @contextmanager
    def fake_tx(*args, **kwargs):
        yield object()

    monkeypatch.setattr(server.db, "tx", fake_tx)
    monkeypatch.setattr(svc, "fetch_categories", lambda conn: ROWS)
    monkeypatch.setattr(svc, "list_accounts", lambda conn: [])
    monkeypatch.setattr(server, "_settings", load_settings({"MCP_TOKEN": "a" * 43}))


EXPECTED_TOOLS = {"get_context", "add_account", "preview_transactions", "save_draft", "find_transactions",
                  "void_transaction"}


@pytest.mark.anyio
async def test_tool_list_is_exactly_what_we_expect():
    """Snapshot of what Claude sees: a new or renamed tool must show up in this test."""
    from app.server import mcp

    async with Client(mcp) as client:
        result = await client.list_tools()
    tools = {t.name: t for t in result.tools}
    assert set(tools) == EXPECTED_TOOLS
    assert tools["get_context"].annotations.read_only_hint is True
    assert tools["find_transactions"].annotations.read_only_hint is True
    assert tools["void_transaction"].annotations.destructive_hint is True
    assert tools["save_draft"].annotations.idempotent_hint is True
    for t in tools.values():
        assert t.description and len(t.description) > 40, f"{t.name} needs a clear description"


@pytest.mark.anyio
async def test_get_context_tool_returns_structured_answer(fake_db):
    from app.server import mcp

    async with Client(mcp) as client:
        result = await client.call_tool("get_context", {})
    assert not result.is_error
    data = result.structured_content
    assert data["timezone"] == "Europe/Amsterdam"
    assert [c["name"] for c in data["categories"]] == ["Coffee", "Rent"]


def test_server_instructions_are_set():
    from app.server import mcp

    assert "personal accountant" in (mcp.instructions or "")
