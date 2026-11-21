"""Token check: every request needs "Authorization: Bearer <token>", except GET /health.

It is an ASGI middleware: a small layer that sees every request before the MCP server does,
like a guard at the building entrance.
"""

import hmac

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

PUBLIC_PATHS = {"/health"}
_PREFIX = b"Bearer "


def is_authorized(header_value: bytes, tokens: list[bytes]) -> bool:
    """Takes the raw Authorization header → compares it with every valid token in constant time → True/False."""
    if not header_value.startswith(_PREFIX):
        return False
    presented = header_value[len(_PREFIX):]
    if not presented:
        return False
    ok = False
    for token in tokens:  # check all of them, so the time taken doesn't reveal which one matched
        ok |= hmac.compare_digest(presented, token)
    return ok


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp, tokens: tuple[str, ...]):
        if not tokens:
            raise ValueError("at least one token is required")  # never run without protection
        self.app = app
        self._tokens = [t.encode() for t in tokens]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)  # startup/shutdown events and /health pass through
            return
        header = next((v for k, v in scope["headers"] if k == b"authorization"), b"")
        if is_authorized(header, self._tokens):
            await self.app(scope, receive, send)
            return
        # Short answer on purpose: no hint about what was wrong.
        response = JSONResponse({"error": "unauthorized"}, status_code=401)
        await response(scope, receive, send)
