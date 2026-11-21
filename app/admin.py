"""One-time and maintenance commands for the database.

Run from the repo folder:
    uv run --env-file .env python -m app.admin migrate
    uv run --env-file .env python -m app.admin create-reader
    uv run --env-file .env python -m app.admin seed-categories ../private-data/categories.yaml
    uv run --env-file .env python -m app.admin check
    uv run python -m app.admin new-token [--next]

Nothing here ever prints a password or a connection string.
"""

import secrets
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
from psycopg import errors, sql

from app.categories_file import load_categories_file
from app.db import READONLY_URL_VAR, connect, get_url

REPO = Path(__file__).resolve().parent.parent
MIGRATIONS = REPO / "migrations"
ENV_FILE = REPO / ".env"
READER = "finance_reader"


def migrate() -> None:
    """Applies every migrations/*.sql file that hasn't been applied yet, each in its own transaction."""
    with connect(autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        pending = [f for f in sorted(MIGRATIONS.glob("*.sql")) if f.name not in applied]
        for f in pending:
            with conn.transaction():
                conn.execute(f.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (f.name,))
            print(f"applied {f.name}")
        if not pending:
            print("nothing to apply, the database is up to date")


def create_reader() -> None:
    """Creates (or re-keys) the finance_reader role and writes READONLY_DATABASE_URL into .env."""
    password = secrets.token_urlsafe(32)
    role = sql.Identifier(READER)
    with connect(autocommit=True) as conn:
        database = conn.execute("SELECT current_database()").fetchone()[0]
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (READER,)).fetchone()
        if exists:
            conn.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(role, sql.Literal(password)))
        else:
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(role, sql.Literal(password)))
        for statement in [
            "GRANT CONNECT ON DATABASE {db} TO {role}",
            "GRANT USAGE ON SCHEMA public TO {role}",
            "GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role}",
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {role}",
            "ALTER ROLE {role} SET default_transaction_read_only = on",
            "ALTER ROLE {role} SET statement_timeout = '3s'",
        ]:
            conn.execute(sql.SQL(statement).format(db=sql.Identifier(database), role=role))

    # Same host and database as DATABASE_URL, but with the reader's name and password.
    parts = urlsplit(get_url())
    host = parts.netloc.rsplit("@", 1)[-1]
    reader_url = urlunsplit(parts._replace(netloc=f"{READER}:{quote(password, safe='')}@{host}"))
    _write_env_value(READONLY_URL_VAR, reader_url)
    print(f"role {READER} {'updated' if exists else 'created'}; {READONLY_URL_VAR} written to .env (not shown)")


def _write_env_value(key: str, value: str) -> None:
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    lines = [line for line in lines if not line.startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def seed_categories(path: str) -> None:
    """Loads categories from a YAML file into the database (adds new ones, updates kind of existing ones)."""
    categories = load_categories_file(path)
    added = updated = 0
    with connect() as conn:
        for c in categories:
            inserted = conn.execute(
                "INSERT INTO categories (name, kind) VALUES (%s, %s) "
                "ON CONFLICT (name) DO UPDATE SET kind = EXCLUDED.kind, is_active = TRUE "
                "RETURNING (xmax = 0)",
                (c.name, c.kind),
            ).fetchone()[0]
            added += inserted
            updated += not inserted
        conn.commit()
    print(f"{len(categories)} categories in the file: {added} added, {updated} already there")


def check() -> int:
    """Proves the setup: tables exist, and finance_reader can read but cannot change anything."""
    results: list[tuple[str, bool]] = []

    with connect() as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )}
        expected = {"categories", "transactions", "items", "drafts", "schema_migrations", "v_expenses"}
        results.append((f"tables and view exist ({', '.join(sorted(expected))})", expected <= tables))
        count = conn.execute("SELECT count(*) FROM categories").fetchone()[0]
        results.append((f"categories loaded ({count})", count > 0))

    # autocommit: every statement is its own transaction, so the SET below really applies to the next one.
    with connect(readonly=True, autocommit=True) as conn:
        conn.execute("SELECT count(*) FROM v_expenses").fetchone()
        results.append(("finance_reader can SELECT", True))
        default_ro = conn.execute("SHOW default_transaction_read_only").fetchone()[0]
        results.append(("finance_reader starts in read-only mode", default_ro == "on"))

        # Switch read-only mode OFF on purpose. Writes must still fail, because the role
        # has only SELECT grants. That is the real protection.
        conn.execute("SET default_transaction_read_only = off")
        attempts = {
            "INSERT": "INSERT INTO categories (name) VALUES ('should-fail')",
            "UPDATE": "UPDATE categories SET is_active = FALSE",
            "DELETE": "DELETE FROM transactions",
            "CREATE TABLE": "CREATE TABLE should_fail (id int)",
        }
        for label, statement in attempts.items():
            try:
                conn.execute(statement)
                blocked = False
            except errors.InsufficientPrivilege:
                blocked = True
            results.append((f"finance_reader {label} is blocked by permissions", blocked))

    for label, ok in results:
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
    return 0 if all(ok for _, ok in results) else 1


def new_token(next_token: bool = False) -> None:
    """Creates a random 43-character token and writes it into .env (never printed).
    --next writes MCP_TOKEN_NEXT, for a swap without downtime: both work until the old one is removed."""
    key = "MCP_TOKEN_NEXT" if next_token else "MCP_TOKEN"
    _write_env_value(key, secrets.token_urlsafe(32))
    print(f"{key} written to .env (not shown)")


def main(argv: list[str]) -> int:
    commands = {"migrate", "create-reader", "seed-categories", "check", "new-token"}
    if not argv or argv[0] not in commands:
        print(f"usage: python -m app.admin {{{'|'.join(sorted(commands))}}} [args]")
        return 2
    if argv[0] == "new-token":
        new_token(next_token="--next" in argv[1:])
        return 0
    try:
        if argv[0] == "migrate":
            migrate()
        elif argv[0] == "create-reader":
            create_reader()
        elif argv[0] == "seed-categories":
            if len(argv) != 2:
                print("usage: python -m app.admin seed-categories PATH_TO_YAML")
                return 2
            seed_categories(argv[1])
        else:
            return check()
    except (psycopg.Error, ValueError) as e:
        # Show the database's message, never the connection string.
        print(f"error: {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
