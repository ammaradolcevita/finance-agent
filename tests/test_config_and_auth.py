"""Step 3: settings fail closed, the token check, and /health. No database needed."""

import pytest
from starlette.testclient import TestClient

from app.auth import is_authorized
from app.config import ConfigError, load_settings

GOOD = "a" * 43
NEXT = "b" * 43


# --- settings -------------------------------------------------------------

def test_missing_token_refuses_to_start():
    with pytest.raises(ConfigError, match="MCP_TOKEN is not set"):
        load_settings({})


def test_short_token_refuses_to_start():
    with pytest.raises(ConfigError, match="too short"):
        load_settings({"MCP_TOKEN": "short"})


def test_error_message_never_contains_the_token():
    weak = "my-weak-secret"
    with pytest.raises(ConfigError) as e:
        load_settings({"MCP_TOKEN": weak})
    assert weak not in str(e.value)


def test_unknown_timezone_refuses_to_start():
    with pytest.raises(ConfigError, match="APP_TIMEZONE"):
        load_settings({"MCP_TOKEN": GOOD, "APP_TIMEZONE": "Mars/Olympus"})


def test_good_settings():
    s = load_settings({"MCP_TOKEN": GOOD, "MCP_TOKEN_NEXT": NEXT, "ALLOWED_HOSTS": "a.example.com, b.example.com"})
    assert s.tokens == (GOOD, NEXT)
    assert s.timezone.key == "Europe/Amsterdam"
    assert s.allowed_hosts == ("a.example.com", "b.example.com")


# --- token comparison -------------------------------------------------------

@pytest.mark.parametrize(
    "header, expected",
    [
        (b"Bearer " + GOOD.encode(), True),
        (b"Bearer " + NEXT.encode(), True),   # second token during a swap
        (b"Bearer " + b"c" * 43, False),       # wrong token
        (b"Bearer ", False),                   # empty token
        (b"", False),                          # no header
        (GOOD.encode(), False),                # token without "Bearer "
        (b"bearer " + GOOD.encode(), False),   # strict prefix
    ],
)
def test_is_authorized(header, expected):
    assert is_authorized(header, [GOOD.encode(), NEXT.encode()]) is expected


# --- the real app over HTTP (in-process) ------------------------------------

@pytest.fixture
def client():
    from app.server import create_app

    app = create_app(load_settings({"MCP_TOKEN": GOOD}))
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        yield c


def test_health_needs_no_token(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_mcp_without_token_is_401(client):
    assert client.post("/mcp", json={}).status_code == 401


def test_mcp_with_wrong_token_is_401(client):
    r = client.post("/mcp", json={}, headers={"Authorization": "Bearer " + "c" * 43})
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}  # no hint about what was wrong


def test_mcp_with_right_token_passes_the_guard(client):
    r = client.post("/mcp", json={}, headers={"Authorization": "Bearer " + GOOD})
    assert r.status_code != 401  # the MCP layer answers (an empty body is not a valid MCP message)


def test_unknown_paths_are_protected_too(client):
    assert client.get("/anything").status_code == 401
