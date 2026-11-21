"""Database connections — the only place that opens them.

Three connection strings come from environment variables (never from the code), one per database role:
- DATABASE_URL           owner: migrations and admin commands only (never used by the server)
- APP_DATABASE_URL       finance_app: what the server uses; can add and change rows, cannot delete
- READONLY_DATABASE_URL  finance_reader: run_query; can only read the views
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

import psycopg

Role = Literal["owner", "app", "reader"]
URL_VARS: dict[str, str] = {
    "owner": "DATABASE_URL",
    "app": "APP_DATABASE_URL",
    "reader": "READONLY_DATABASE_URL",
}
MAIN_URL_VAR = URL_VARS["owner"]
APP_URL_VAR = URL_VARS["app"]
READONLY_URL_VAR = URL_VARS["reader"]


def get_url(role: Role = "owner") -> str:
    """Return the connection string for a role from env, or stop with a clear message (never printing it)."""
    var = URL_VARS[role]
    url = os.environ.get(var, "").strip()
    if not url:
        raise SystemExit(f"{var} is not set. Put it in .env and run with: uv run --env-file .env ...")
    return url


def connect(role: Role = "owner", autocommit: bool = False) -> psycopg.Connection:
    """Open a connection as the given role. One retry: a sleeping Neon database may need a moment to wake."""
    url = get_url(role)
    try:
        return psycopg.connect(url, autocommit=autocommit, connect_timeout=10)
    except psycopg.OperationalError:
        return psycopg.connect(url, autocommit=autocommit, connect_timeout=10)


@contextmanager
def tx(role: Role = "app") -> Iterator[psycopg.Connection]:
    """One tool call = one database transaction: commit if everything worked, roll back on any error."""
    conn = connect(role)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
