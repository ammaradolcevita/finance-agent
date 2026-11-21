"""Database connections.

Two connection strings come from environment variables (never from the code):
- DATABASE_URL           main role, can read and write
- READONLY_DATABASE_URL  finance_reader role, can only SELECT (used by run_query)
"""

import os

import psycopg

MAIN_URL_VAR = "DATABASE_URL"
READONLY_URL_VAR = "READONLY_DATABASE_URL"


def get_url(readonly: bool = False) -> str:
    """Return the connection string from env, or stop with a clear message (without printing it)."""
    var = READONLY_URL_VAR if readonly else MAIN_URL_VAR
    url = os.environ.get(var, "").strip()
    if not url:
        raise SystemExit(f"{var} is not set. Put it in .env and run with: uv run --env-file .env ...")
    return url


def connect(readonly: bool = False, autocommit: bool = False) -> psycopg.Connection:
    """Open a connection as the main role, or as finance_reader if readonly=True."""
    return psycopg.connect(get_url(readonly), autocommit=autocommit, connect_timeout=15)
