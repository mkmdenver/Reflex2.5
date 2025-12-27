# common/dbcore.py
from __future__ import annotations
import contextlib
import psycopg2
from psycopg2 import pool
from typing import Iterator, Optional
from .creds import get_pg_dsn
from .app_logging import get_logger

log = get_logger("DBCore")

# Simple global connection pool
_POOL: Optional[pool.SimpleConnectionPool] = None

def init_pool(minconn: int = 1, maxconn: int = 8) -> None:
    global _POOL
    if _POOL is None:
        dsn = get_pg_dsn()
        _POOL = pool.SimpleConnectionPool(minconn, maxconn, dsn=dsn)
        log.info("PostgreSQL pool initialized (%d..%d)", minconn, maxconn)

@contextlib.contextmanager
def get_conn():
    """
    Context manager that returns a pooled connection.
    Ensures autocommit behavior for admin/DDL heavy workflows in dbmanager.
    """
    if _POOL is None:
        init_pool()
    assert _POOL is not None
    conn = _POOL.getconn()
    try:
        conn.autocommit = True
        yield conn
    finally:
        _POOL.putconn(conn)

@contextlib.contextmanager
def get_cursor():
    with get_conn() as conn:
        with conn.cursor() as cur:
            yield cur
