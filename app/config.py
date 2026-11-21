"""Settings from environment variables, checked once when the server starts.

If something important is missing or weak, the server refuses to start (fail closed),
instead of starting without protection.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MIN_TOKEN_LENGTH = 32
DEFAULT_TIMEZONE = "Europe/Amsterdam"


class ConfigError(RuntimeError):
    """A setting is missing or invalid. The message says which one, never its value."""


@dataclass(frozen=True)
class Settings:
    tokens: tuple[str, ...]            # MCP_TOKEN, plus MCP_TOKEN_NEXT during a token swap
    timezone: ZoneInfo                 # "today" and "this month" are calculated in this zone
    allowed_hosts: tuple[str, ...]     # public host names of the server (empty = local only)


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    """Takes the environment → checks tokens, time zone, hosts → returns Settings or raises ConfigError."""
    tokens: list[str] = []
    for var in ("MCP_TOKEN", "MCP_TOKEN_NEXT"):
        value = env.get(var, "").strip()
        if not value:
            if var == "MCP_TOKEN":
                raise ConfigError("MCP_TOKEN is not set. Create one with: python -m app.admin new-token")
            continue
        if len(value) < MIN_TOKEN_LENGTH:
            raise ConfigError(f"{var} is too short: it needs at least {MIN_TOKEN_LENGTH} characters")
        tokens.append(value)

    tz_name = env.get("APP_TIMEZONE", "").strip() or DEFAULT_TIMEZONE
    try:
        timezone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ConfigError(f"APP_TIMEZONE '{tz_name}' is not a known time zone") from e

    hosts = [h.strip() for h in env.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
    render_host = env.get("RENDER_EXTERNAL_HOSTNAME", "").strip()   # set automatically by Render
    if render_host and render_host not in hosts:
        hosts.append(render_host)
    return Settings(tokens=tuple(tokens), timezone=timezone, allowed_hosts=tuple(hosts))
