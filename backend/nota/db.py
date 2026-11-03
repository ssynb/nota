"""Postgres access: one pool per process, explicit transactions, schema applied once under a lock."""
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from nota import config

SCHEMA_LOCK = 7_200_001
_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=20, kwargs={"row_factory": dict_row}, open=True)
    return _pool


@contextmanager
def tx():
    """A connection inside one transaction; commits on success, rolls back on error."""
    with pool().connection() as conn, conn.transaction():
        yield conn


def connect() -> psycopg.Connection:
    """A dedicated autocommit connection (LISTEN loops)."""
    return psycopg.connect(config.DATABASE_URL, autocommit=True, row_factory=dict_row)


def ensure_schema():
    sql = (Path(__file__).parent / "schema.sql").read_text()
    with tx() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
        conn.execute(sql)


json = Jsonb
