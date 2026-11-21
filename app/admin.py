"""One-time and maintenance commands for the database. They run as the owner, from the laptop.

Run from the repo folder (the practice branch uses .env, production uses .env.prod):
    uv run --env-file .env python -m app.admin migrate
    uv run --env-file .env python -m app.admin create-roles
    uv run --env-file .env python -m app.admin seed-categories ../private-data/categories.yaml
    uv run --env-file .env python -m app.admin category-kind "<name>" excluded
    uv run --env-file .env python -m app.admin check
    uv run python -m app.admin new-token [--next]
For production, put ADMIN_ENV_FILE=.env.prod in front, so new passwords are written to .env.prod.

Nothing here ever prints a password, a token or a connection string.
"""

import hashlib
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
from psycopg import errors, sql

from app.categories_file import load_categories_file
from app.db import APP_URL_VAR, READONLY_URL_VAR, connect, get_url

REPO = Path(__file__).resolve().parent.parent
MIGRATIONS = REPO / "migrations"
ENV_FILE = REPO / os.environ.get("ADMIN_ENV_FILE", ".env")
APP_ROLE = "finance_app"
READER = "finance_reader"

APP_TABLES_READ_WRITE = ["accounts", "categories", "transactions", "items", "drafts"]
READER_VIEWS = ["v_movements", "v_account_balances", "v_spending", "v_income", "v_items",
                "v_accounts", "v_categories"]


# --- migrations ---------------------------------------------------------------

def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def migrate() -> None:
    """Applies every migrations/*.sql not applied yet, each in its own transaction, and remembers its checksum.
    If an already applied file was edited afterwards, it stops: applied migrations must never change."""
    with connect(autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        conn.execute("ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum TEXT")
        applied = dict(conn.execute("SELECT version, checksum FROM schema_migrations").fetchall())
        files = sorted(MIGRATIONS.glob("*.sql"))
        for f in files:
            if f.name in applied:
                if applied[f.name] is None:  # applied before checksums existed: record it now
                    conn.execute("UPDATE schema_migrations SET checksum = %s WHERE version = %s",
                                 (_checksum(f), f.name))
                elif applied[f.name] != _checksum(f):
                    raise SystemExit(f"{f.name} was changed after it was applied. "
                                     "Never edit an applied migration: write a new one.")
        pending = [f for f in files if f.name not in applied]
        for f in pending:
            with conn.transaction():
                conn.execute(f.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                             (f.name, _checksum(f)))
            print(f"applied {f.name}")
        if not pending:
            print("nothing to apply, the database is up to date")


# --- roles ----------------------------------------------------------------------

def _url_for(role_name: str, password: str) -> str:
    """Same host and database as DATABASE_URL, but with another role's name and password."""
    parts = urlsplit(get_url("owner"))
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit(parts._replace(netloc=f"{role_name}:{quote(password, safe='')}@{host}"))


def _create_or_rekey(conn: psycopg.Connection, role_name: str) -> tuple[str, bool]:
    password = secrets.token_urlsafe(32)
    role = sql.Identifier(role_name)
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,)).fetchone() is not None
    verb = "ALTER" if exists else "CREATE"
    conn.execute(sql.SQL(verb + " ROLE {} LOGIN PASSWORD {}").format(role, sql.Literal(password)))
    return password, exists


def create_roles() -> None:
    """Creates (or re-keys) both roles with least privilege and writes their URLs into the env file:
    - finance_app: SELECT/INSERT/UPDATE on the working tables, change_log add-only, never DELETE or DDL
    - finance_reader: SELECT on the views only (run_query)"""
    with connect(autocommit=True) as conn:
        db = sql.Identifier(conn.execute("SELECT current_database()").fetchone()[0])
        app_pw, app_existed = _create_or_rekey(conn, APP_ROLE)
        reader_pw, reader_existed = _create_or_rekey(conn, READER)
        app, reader = sql.Identifier(APP_ROLE), sql.Identifier(READER)

        def run(statement: str, **names: sql.Composable) -> None:
            conn.execute(sql.SQL(statement).format(db=db, app=app, reader=reader, **names))

        run("REVOKE TEMPORARY ON DATABASE {db} FROM PUBLIC")
        for role in (app, reader):
            conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(db, role))
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))

        # finance_app
        for table in APP_TABLES_READ_WRITE:
            run("GRANT SELECT, INSERT, UPDATE ON {t} TO {app}", t=sql.Identifier(table))
        run("GRANT SELECT, INSERT ON change_log TO {app}")             # history: add only
        for view in READER_VIEWS:
            run("GRANT SELECT ON {v} TO {app}", v=sql.Identifier(view))
        run("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {app}")
        run("ALTER ROLE {app} SET statement_timeout = '10s'")

        # finance_reader: views only (old broad grants removed)
        run("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {reader}")
        run("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM {reader}")
        for view in READER_VIEWS:
            run("GRANT SELECT ON {v} TO {reader}", v=sql.Identifier(view))
        run("GRANT SELECT ON change_log TO {reader}")
        run("ALTER ROLE {reader} SET default_transaction_read_only = on")
        run("ALTER ROLE {reader} SET statement_timeout = '3s'")

    _write_env_value(APP_URL_VAR, _url_for(APP_ROLE, app_pw))
    _write_env_value(READONLY_URL_VAR, _url_for(READER, reader_pw))
    print(f"role {APP_ROLE} {'re-keyed' if app_existed else 'created'}, "
          f"role {READER} {'re-keyed' if reader_existed else 'created'}; "
          f"{APP_URL_VAR} and {READONLY_URL_VAR} written to {ENV_FILE.name} (not shown)")


def _write_env_value(key: str, value: str) -> None:
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    lines = [line for line in lines if not line.startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- categories -----------------------------------------------------------------

def seed_categories(path: str) -> None:
    """Loads categories from a YAML file (adds new ones, updates the kind of existing ones). Prints only counts."""
    categories = load_categories_file(path)
    added = updated = 0
    with connect() as conn:
        for c in categories:
            inserted = conn.execute(
                "INSERT INTO categories (name, kind) VALUES (%s, %s) "
                "ON CONFLICT ((lower(name))) DO UPDATE SET kind = EXCLUDED.kind, is_active = TRUE "
                "RETURNING (xmax = 0)",
                (c.name, c.kind),
            ).fetchone()[0]
            added += inserted
            updated += not inserted
        conn.commit()
    print(f"{len(categories)} categories in the file: {added} added, {updated} already there")


def category_kind(name: str, kind: str) -> None:
    """Sets the kind of one category (regular / structural / excluded). The name is given on the command line,
    so no private category name is ever written into this public file."""
    if kind not in {"regular", "structural", "excluded"}:
        raise ValueError("kind must be regular, structural or excluded")
    with connect() as conn:
        row = conn.execute("UPDATE categories SET kind = %s WHERE lower(name) = lower(%s) RETURNING id",
                           (kind, name)).fetchone()
        conn.commit()
    print("updated 1 category" if row else "no category with that name")


# --- check ------------------------------------------------------------------------

def _blocked(conn: psycopg.Connection, statement: str) -> bool:
    try:
        conn.execute(statement)
        return False
    except errors.InsufficientPrivilege:
        return True


def check() -> int:
    """Proves the setup: tables and views exist; finance_app can work but can't delete or change tables;
    finance_reader can read the views and nothing else, even with read-only mode switched off."""
    results: list[tuple[str, bool]] = []

    with connect() as conn:
        names = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
        expected = {"accounts", "categories", "transactions", "items", "drafts", "change_log",
                    "schema_migrations", *READER_VIEWS}
        missing = expected - names
        results.append((f"all tables and views exist ({len(expected)})", not missing))
        count = conn.execute("SELECT count(*) FROM categories").fetchone()[0]
        results.append((f"categories loaded ({count})", count > 0))
        tz = conn.execute("SHOW timezone").fetchone()[0]
        results.append((f"database time zone is Europe/Amsterdam ({tz})", tz == "Europe/Amsterdam"))

    with connect("app", autocommit=True) as conn:
        conn.execute("SELECT count(*) FROM transactions").fetchone()
        results.append(("finance_app can read", True))
        for label, statement in {
            "DELETE": "DELETE FROM drafts WHERE false",
            "TRUNCATE": "TRUNCATE drafts",
            "change the history": "UPDATE change_log SET action = action WHERE false",
            "CREATE TABLE": "CREATE TABLE should_fail (id int)",
            "DROP TABLE": "DROP TABLE drafts",
        }.items():
            results.append((f"finance_app {label} is blocked", _blocked(conn, statement)))

    with connect("reader", autocommit=True) as conn:
        conn.execute("SELECT count(*) FROM v_spending").fetchone()
        results.append(("finance_reader can read the views", True))
        results.append(("finance_reader cannot read raw tables or drafts",
                        _blocked(conn, "SELECT count(*) FROM drafts")
                        and _blocked(conn, "SELECT count(*) FROM transactions")))
        default_ro = conn.execute("SHOW default_transaction_read_only").fetchone()[0]
        results.append(("finance_reader starts in read-only mode", default_ro == "on"))
        conn.execute("SET default_transaction_read_only = off")   # switched off on purpose
        for label, statement in {
            "INSERT": "INSERT INTO categories (name) VALUES ('should-fail')",
            "UPDATE": "UPDATE categories SET is_active = FALSE",
            "DELETE": "DELETE FROM transactions",
            "CREATE TABLE": "CREATE TABLE should_fail (id int)",
        }.items():
            results.append((f"finance_reader {label} is blocked by permissions", _blocked(conn, statement)))

    for label, ok in results:
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
    return 0 if all(ok for _, ok in results) else 1


# --- tokens -----------------------------------------------------------------------

def new_token(next_token: bool = False) -> None:
    """Creates a random 43-character token and writes it into the env file (never printed).
    --next writes MCP_TOKEN_NEXT, for a swap without downtime: both work until the old one is removed."""
    key = "MCP_TOKEN_NEXT" if next_token else "MCP_TOKEN"
    _write_env_value(key, secrets.token_urlsafe(32))
    print(f"{key} written to {ENV_FILE.name} (not shown)")


def main(argv: list[str]) -> int:
    commands = {"migrate", "create-roles", "seed-categories", "category-kind", "check", "new-token"}
    if not argv or argv[0] not in commands:
        print(f"usage: python -m app.admin {{{'|'.join(sorted(commands))}}} [args]")
        return 2
    command, args = argv[0], argv[1:]
    try:
        if command == "new-token":
            new_token(next_token="--next" in args)
        elif command == "migrate":
            migrate()
        elif command == "create-roles":
            create_roles()
        elif command == "seed-categories":
            if len(args) != 1:
                print("usage: python -m app.admin seed-categories PATH_TO_YAML")
                return 2
            seed_categories(args[0])
        elif command == "category-kind":
            if len(args) != 2:
                print('usage: python -m app.admin category-kind "NAME" regular|structural|excluded')
                return 2
            category_kind(args[0], args[1])
        else:
            return check()
    except (psycopg.Error, ValueError) as e:
        # Show the database's message, never the connection string.
        print(f"error: {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
