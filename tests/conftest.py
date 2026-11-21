"""Shared test setup.

Database tests use the practice branch (APP_DATABASE_URL from .env) and run inside one transaction that is
always rolled back, so they leave nothing behind. Safety lock: they refuse to run if the database host is the
production host from .env.prod. Run them with:  uv run --env-file .env pytest
Without the env file they are skipped (pure tests still run).
"""

import os
import re
from pathlib import Path

import psycopg
import pytest

REPO = Path(__file__).resolve().parent.parent


def _host(url: str | None) -> str | None:
    m = re.search(r"@([^/:?]+)", url or "")
    return m.group(1) if m else None


def _production_host() -> str | None:
    env_prod = REPO / ".env.prod"
    if not env_prod.exists():
        return None
    m = re.search(r"^DATABASE_URL=(.*)$", env_prod.read_text(encoding="utf-8"), re.M)
    return _host(m.group(1)) if m else None


@pytest.fixture
def db_conn():
    url = os.environ.get("APP_DATABASE_URL")
    if not url:
        pytest.skip("no APP_DATABASE_URL: run database tests with `uv run --env-file .env pytest`")
    if os.environ.get("TEST_DATABASE_IS_THROWAWAY") != "1":   # CI sets this for its temporary database
        production = _production_host()
        if production is None:
            pytest.skip("no .env.prod to compare with: refusing to guess which database this is")
        if _host(url) == production:
            pytest.exit("APP_DATABASE_URL points to PRODUCTION: database tests refused", returncode=2)
    conn = psycopg.connect(url)
    try:
        yield conn
    finally:
        conn.rollback()   # nothing a test did is kept
        conn.close()


@pytest.fixture
def world(db_conn):
    """A small made-up setup (2 accounts, 3 categories) on top of db_conn; returns (conn, now)."""
    from datetime import datetime

    from tests.helpers import AMS, make_world

    now = datetime.now(AMS)
    make_world(db_conn, now)
    return db_conn, now
