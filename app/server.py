"""The MCP server: registers tools, adds /health and the token check, starts HTTP.

Tools here are thin: read input → call a service → return a typed answer.
Run locally:  uv run --env-file .env python -m app.server
"""

import functools
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

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
from app.models import ContextOut
from app.services import context as context_service

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


@mcp.tool(annotations=ToolAnnotations(title="Get context", read_only_hint=True))
@logged_tool
def get_context() -> ContextOut:
    """Call this first in every chat. Returns today's date and weekday in the user's time zone,
    the accounts (with what each is for) and the active categories (with their kind)."""
    with db.connect() as conn:
        return context_service.get_context(conn, settings().timezone)


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
