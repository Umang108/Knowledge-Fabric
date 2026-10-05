"""Postgres connection pool and a minimal SQL-file migration runner."""

from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import get_settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_LOCK = 72_011_001  # arbitrary advisory-lock id

_pool: ConnectionPool | None = None


def open_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            get_settings().postgres_dsn, min_size=1, max_size=10, kwargs={"row_factory": dict_row}, open=True
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def get_conn():
    """A pooled connection; commits on success, rolls back on error."""
    with open_pool().connection() as conn:
        yield conn


def run_migrations(dsn: str | None = None) -> list[str]:
    """Apply migrations/NNN_*.sql in order, each once. Safe to call concurrently."""
    applied = []
    with psycopg.connect(dsn or get_settings().postgres_dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK,))
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                                version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())""")
            done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.stem in done:
                    continue
                with conn.transaction():
                    conn.execute(path.read_text())
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
                applied.append(path.stem)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK,))
    return applied
