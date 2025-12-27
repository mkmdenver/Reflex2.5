"""
Reflex2 Unified DB Layer (psycopg v3 + psycopg_pool)
---------------------------------------------------
Single source of truth for database access across all services.

Goals
- One place to create and manage a connection pool
- Simple, safe helpers for queries and bulk writes
- Timescale/"public" search_path enforced per-connection
- Drop-in compatibility shims for existing dbutils.* callers

Usage
-----
from reflex2_db_layer import db

# 1) Initialize once at process start (optional – lazy by default)
db.configure(
    dsn=os.getenv("DATABASE_URL"),  # or leave None to auto-read
    pool_min=1,
    pool_max=10,
    timeout=30.0,
)

# 2) Use helpers
rows = db.fetch_all("SELECT 1 AS x")
count = db.execute("DELETE FROM tick WHERE ts < now() - interval '7 days'")

# 3) Bulk upsert
cols = ["symbol", "ts", "price"]
data = [("AAPL", datetime.now(timezone.utc), 199.12)]
upserted = db.bulk_upsert(
    table="tick", columns=cols, rows=data,
    conflict_cols=["symbol", "ts"], update_cols=["price"],
)

Environment
-----------
DATABASE_URL or POSTGRES_DSN (preferred: DATABASE_URL)
REFLEX_DB_POOL_MIN (default 1)
REFLEX_DB_POOL_MAX (default 10)
REFLEX_DB_POOL_TIMEOUT (seconds, default 30)
REFLEX_DB_SEARCH_PATH (default "public, pg_catalog")

Thread/process model
--------------------
psycopg_pool.ConnectionPool is safe to share across threads in a single process.
For multiprocess deployments (gunicorn, etc.), create the pool per-process.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping, Sequence
import os
import threading

try:
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
except ImportError:
    import subprocess
    import sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "psycopg", "psycopg_pool"])
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

# --------------------------------------------------------------------------------------
# Configuration & Singletons
# --------------------------------------------------------------------------------------
@dataclass
class _Config:
    dsn: str | None = None
    pool_min: int = int(os.getenv("REFLEX_DB_POOL_MIN", 1))
    pool_max: int = int(os.getenv("REFLEX_DB_POOL_MAX", 10))
    timeout: float = float(os.getenv("REFLEX_DB_POOL_TIMEOUT", 30.0))
    search_path: str = os.getenv("REFLEX_DB_SEARCH_PATH", "public, pg_catalog")

_cfg = _Config()
_pool_lock = threading.Lock()
_pool: ConnectionPool | None = None


def _resolve_dsn() -> str:
    dsn = _cfg.dsn or os.getenv("DATABASE_URL") or os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("DATABASE_URL or POSTGRES_DSN must be set for DB access")
    return dsn


def configure(*, dsn: str | None = None, pool_min: int | None = None, pool_max: int | None = None, timeout: float | None = None, search_path: str | None = None) -> None:
    """Optionally configure the pool before first use."""
    if dsn is not None:
        _cfg.dsn = dsn
    if pool_min is not None:
        _cfg.pool_min = pool_min
    if pool_max is not None:
        _cfg.pool_max = pool_max
    if timeout is not None:
        _cfg.timeout = timeout
    if search_path is not None:
        _cfg.search_path = search_path


def _ensure_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(
                    conninfo=_resolve_dsn(),
                    min_size=max(1, _cfg.pool_min),
                    max_size=max(_cfg.pool_min, _cfg.pool_max),
                    timeout=_cfg.timeout,
                    kwargs={"autocommit": True},
                )
    return _pool


# --------------------------------------------------------------------------------------
# Connection / Cursor helpers
# --------------------------------------------------------------------------------------
@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """Get a pooled connection with search_path enforced."""
    pool = _ensure_pool()
    with pool.connection() as conn:  # autocommit=True from pool kwargs
        with conn.cursor(row_factory=dict_row) as cur:
            _ensure_search_path(cur)
        yield conn


@contextmanager
def cursor() -> Iterator[psycopg.Cursor]:
    """Yield a cursor from a pooled connection (dict_row)."""
    with connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            _ensure_search_path(cur)
            yield cur


def _ensure_search_path(cur: psycopg.Cursor) -> None:
    sp = _cfg.search_path
    cur.execute("SET search_path = " + sp)


# --------------------------------------------------------------------------------------
# Public query APIs (simple & ergonomic)
# --------------------------------------------------------------------------------------

def fetch_all(query: str, params: Sequence[Any] | None = None) -> list[Mapping[str, Any]]:
    with cursor() as cur:
        cur.execute(query, params or [])
        return list(cur.fetchall())


def fetch_one(query: str, params: Sequence[Any] | None = None) -> Mapping[str, Any] | None:
    with cursor() as cur:
        cur.execute(query, params or [])
        return cur.fetchone()


def execute(query: str, params: Sequence[Any] | None = None) -> int:
    """Execute a single statement; returns cur.rowcount (when available)."""
    with cursor() as cur:
        cur.execute(query, params or [])
        try:
            return cur.rowcount or 0
        except Exception:
            return 0


def ExecuteSQLBlock(query: str, params_iter: Iterable[Sequence[Any]], *, chunk_size: int = 1000, on_error: str = "raise") -> int:
    """Efficient batched executemany with basic error handling policies.

    on_error: "raise" | "continue" | "skip-bad"
    Returns number of parameter sets *attempted*.
    """
    attempted = 0
    batch: list[Sequence[Any]] = []
    with cursor() as cur:
        for params in params_iter:
            batch.append(params)
            if len(batch) >= chunk_size:
                attempted += _flush_batch(cur, query, batch, on_error)
                batch = []
        if batch:
            attempted += _flush_batch(cur, query, batch, on_error)
    return attempted


def _flush_batch(cur: psycopg.Cursor, query: str, batch: list[Sequence[Any]], on_error: str) -> int:
    tried = False
    try:
        cur.executemany(query, batch)
        tried = True
        return len(batch)
    except Exception:
        if on_error == "raise":
            raise
        if on_error == "continue":
            return 0
        if on_error == "skip-bad":
            attempted = 0
            for row in batch:
                try:
                    cur.execute(query, row)
                    attempted += 1
                except Exception:
                    pass
            return attempted
        raise


# --------------------------------------------------------------------------------------
# Bulk UPSERT helper (portable and indexes-friendly)
# --------------------------------------------------------------------------------------

def bulk_upsert(
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]] | Iterable[Mapping[str, Any]],
    *,
    conflict_cols: Sequence[str],
    update_cols: Sequence[str] | None = None,
    chunk_size: int = 1000,
) -> int:
    """Perform batched INSERT ... ON CONFLICT DO UPDATE.

    - `rows` may be a sequence-of-sequences aligned to `columns`, **or** an
      iterable of mappings (dict-like) providing values by column name.
    - Returns total attempted row count (actual rowcount semantics vary for upserts).
    """
    if not columns:
        raise ValueError("columns must be non-empty")
    if not conflict_cols:
        raise ValueError("conflict_cols must be non-empty")

    if update_cols is None:
        update_cols = [c for c in columns if c not in set(conflict_cols)]

    # Build statement with psycopg.sql composition for safety
    tbl = sql.Identifier(table)
    col_idents = [sql.Identifier(c) for c in columns]

    insert_stmt = sql.SQL("INSERT INTO {tbl} ({cols}) VALUES ({vals}) ON CONFLICT ({conflict}) DO UPDATE SET {updates}").format(
        tbl=tbl,
        cols=sql.SQL(", ").join(col_idents),
        vals=sql.SQL(", ").join(sql.Placeholder() * len(columns)),
        conflict=sql.SQL(", ").join(sql.Identifier(c) for c in conflict_cols),
        updates=sql.SQL(", ").join(
            sql.SQL("{c}=EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in update_cols
        ),
    )

    def _iter_params() -> Iterator[Sequence[Any]]:
        if not rows:
            return
        if isinstance(rows, Iterable) and rows and isinstance(next(iter(rows)), Mapping):
            # rows is an iterable of dicts/mappings
            for r in rows:  # type: ignore[assignment]
                yield [r.get(c) for c in columns]  # order matches `columns`
        else:
            for r in rows:  # type: ignore[assignment]
                yield list(r)

    attempted = 0
    batch: list[Sequence[Any]] = []
    with cursor() as cur:
        for params in _iter_params() or []:
            batch.append(params)
            if len(batch) >= chunk_size:
                attempted += _flush_batch(cur, insert_stmt, batch, on_error="raise")
                batch = []
        if batch:
            attempted += _flush_batch(cur, insert_stmt, batch, on_error="raise")
    return attempted


# --------------------------------------------------------------------------------------
# Health & Admin
# --------------------------------------------------------------------------------------

def ping() -> bool:
    try:
        one = fetch_one("SELECT 1 AS ok")
        return bool(one and one.get("ok") == 1)
    except Exception:
        return False


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


# --------------------------------------------------------------------------------------
# Backwards-compat shims for existing code (dbutils.*)
# --------------------------------------------------------------------------------------
# These keep older imports running while you migrate call sites progressively.

class db:
    """Namespace-style export: import as `from reflex2_db_layer import db`"""

    configure = staticmethod(configure)
    fetch_all = staticmethod(fetch_all)
    fetch_one = staticmethod(fetch_one)
    execute = staticmethod(execute)
    execute_many = staticmethod(ExecuteSQLBlock)
    bulk_upsert = staticmethod(bulk_upsert)
    connection = staticmethod(connection)
    cursor = staticmethod(cursor)
    ping = staticmethod(ping)
    close_pool = staticmethod(close_pool)


# Legacy names (minimal surface to avoid churn)
# If you previously did: from common.dbutils import execute_many, conn_cursor, dsn
# these adapters will continue to work while you refactor.

@contextmanager
def conn_cursor() -> Iterator[psycopg.Cursor]:
    with cursor() as cur:
        yield cur


def dsn() -> str:
    return _resolve_dsn()
