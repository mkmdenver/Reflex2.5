# =============================================================================
# common/dbLayer/dbutils.py
# Version: 2025.10.22-rc2
#
# CHANGELOG
# - 2025-10-22: rc2
#   • Restore bulk_upsert for compatibility with db_writer.py.
#   • bulk_upsert: accepts DataFrame / list[dict] / list[tuple], batched executemany.
#   • Validates target columns; clearer errors; masks secrets in logs.
# - 2025-10-22: rc1
#   • Unify DSN resolution (DATABASE_URL -> POSTGRES_DSN -> PG* vars).
#   • load_dotenv; improved retry logging; DSN masked.
# =============================================================================
from __future__ import annotations
import os
import time
import logging
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import quote

from dotenv import load_dotenv
import psycopg
from psycopg import sql
from psycopg.rows import tuple_row

try:
    import pandas as pd  # type: ignore
except Exception:  # pandas optional
    pd = None  # noqa

LOG = logging.getLogger("common.dbLayer.dbutils")
load_dotenv()  # ensure .env is loaded for any caller

# -----------------------------------------------------------------------------#
# DSN helpers
# -----------------------------------------------------------------------------#

def _mask_dsn(dsn: str) -> str:
    try:
        if "://" in dsn and "@" in dsn:
            prefix, tail = dsn.split("://", 1)
            creds, rest = tail.split("@", 1)
            if ":" in creds:
                user, _ = creds.split(":", 1)
                return f"{prefix}://{user}:***@{rest}"
    except Exception:
        pass
    return dsn

def _dsn_from_env() -> str:
    dsn = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_DSN")
    if dsn:
        return dsn
    # Compose from libpq vars
    host = os.getenv("PGHOST", "localhost")
    port = os.getenv("PGPORT", "5432")
    user = os.getenv("PGUSER", "postgres")
    pwd  = os.getenv("PGPASSWORD", "")
    db   = os.getenv("PGDATABASE", "postgres")
    # Build URL-safe DSN (encode special chars)
    return f"postgresql://{user}:{quote(pwd, safe='')}@{host}:{port}/{db}"

# -----------------------------------------------------------------------------#
# Connections
# -----------------------------------------------------------------------------#

def connection(retries: int = 3, backoff: float = 0.25) -> psycopg.Connection:
    failures: List[str] = []
    for attempt in range(1, retries + 1):
        dsn = _dsn_from_env()
        try:
            conn = psycopg.connect(dsn)
            if attempt > 1:
                LOG.warning("DB connect succeeded after %d attempt(s). DSN=%s", attempt, _mask_dsn(dsn))
            return conn
        except Exception as e:
            msg = f"connection failed: {e}"
            failures.append(msg)
            if attempt < retries:
                LOG.warning(
                    "DB connect failed (attempt %d/%d): %s; retrying in %.2fs",
                    attempt, retries, msg, backoff
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 4.0)
            else:
                LOG.error("DB connect failed after %d attempts: %s", retries, "\n".join(failures))
                raise

# -----------------------------------------------------------------------------#
# Introspection helpers
# -----------------------------------------------------------------------------#

def table_columns(conn: psycopg.Connection, table: str) -> List[str]:
    """Return column names (lowercase) for a given table (optionally schema-qualified)."""
    if "." in table:
        schema, name = table.split(".", 1)
    else:
        schema, name = "public", table
    q = """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
    """
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(q, (schema, name))
        rows = cur.fetchall()
    return [r[0].lower() for r in rows]

# -----------------------------------------------------------------------------#
# Bulk upsert
# -----------------------------------------------------------------------------#

RowsT = Union[
    "pd.DataFrame",                 # type: ignore
    Sequence[Mapping[str, object]], # list[dict]
    Sequence[Sequence[object]],     # list[tuple]
]

def bulk_upsert(
    conn: psycopg.Connection,
    table: str,
    rows: RowsT,
    columns: Optional[List[str]] = None,
    conflict_cols: Optional[List[str]] = None,
    update_cols: Optional[List[str]] = None,
    chunk_size: int = 5_000,
) -> int:
    """
    Bulk UPSERT into `table` using INSERT ... ON CONFLICT.

    Parameters
    ----------
    conn : psycopg.Connection
    table : str
        Target table, optionally schema-qualified (e.g., "public.minute_bars").
    rows : DataFrame | list[dict] | list[tuple]
        Rows to upsert. If DataFrame, its columns are used unless `columns` is provided.
        If list[dict], keys must match column names.
        If list[tuple], you must supply `columns`.
    columns : list[str] | None
        Column order for INSERT. If None and rows is DataFrame → use df.columns.
    conflict_cols : list[str] | None
        Columns defining ON CONFLICT constraint. If None, try PRIMARY KEY.
    update_cols : list[str] | None
        Columns to update on conflict. If None, use (columns - conflict_cols).
    chunk_size : int
        Executemany batch size.

    Returns
    -------
    int : number of rows attempted (len(rows)).
    """
    # Normalize rows/columns
    df = None
    if pd is not None and hasattr(rows, "to_dict"):
        df = rows  # type: ignore
    if df is not None:
        if columns is None:
            columns = [str(c) for c in df.columns]
        # materialize as list of tuples in the correct order
        data_seq: Sequence[Sequence[object]] = [tuple(rec) for rec in df[columns].itertuples(index=False, name=None)]  # type: ignore
    elif isinstance(rows, Sequence) and rows and isinstance(rows[0], Mapping):  # list[dict]
        if columns is None:
            # union of keys, but keep deterministic ordering by first row
            columns = list(rows[0].keys())  # type: ignore
        data_seq = [tuple((r.get(c) for c in columns)) for r in rows]  # type: ignore
    else:
        # treat as list[tuple]
        if columns is None:
            raise ValueError("columns must be provided when rows is list[tuple]")
        data_seq = rows  # type: ignore

    if not columns:
        raise ValueError("No columns resolved for bulk_upsert()")
    if not data_seq:
        return 0

    # Validate columns exist
    tgt_cols = set([c.lower() for c in table_columns(conn, table)])
    miss = [c for c in columns if c.lower() not in tgt_cols]
    if miss:
        raise ValueError(f"bulk_upsert columns not in target {table}: {miss}")

    # Resolve conflict/update columns
    if conflict_cols is None or len(conflict_cols) == 0:
        # Try to detect PK from pg_catalog
        conflict_cols = _primary_key_cols(conn, table)
        if not conflict_cols:
            raise ValueError("conflict_cols not provided and primary key could not be determined")
    if update_cols is None or len(update_cols) == 0:
        update_cols = [c for c in columns if c not in conflict_cols]

    # Build SQL
    ident_table = _ident_table(table)
    idents_cols = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
    placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in columns)

    insert_stmt = sql.SQL("INSERT INTO {table} ({cols}) VALUES ({vals})").format(
        table=ident_table, cols=idents_cols, vals=placeholders
    )

    if update_cols:
        set_clause = sql.SQL(", ").join(
            sql.Composed([sql.Identifier(c), sql.SQL(" = EXCLUDED."), sql.Identifier(c)]) for c in update_cols
        )
    else:
        # Do nothing on conflict if no updates requested
        set_clause = sql.SQL("")

    conflict_clause = sql.SQL(", ").join(sql.Identifier(c) for c in conflict_cols)
    if update_cols:
        on_conflict = sql.SQL(" ON CONFLICT ({conflict}) DO UPDATE SET {set_clause}").format(
            conflict=conflict_clause, set_clause=set_clause
        )
    else:
        on_conflict = sql.SQL(" ON CONFLICT ({conflict}) DO NOTHING").format(conflict=conflict_clause)

    stmt = sql.Composed([insert_stmt, on_conflict])

    # Execute in chunks
    total = 0
    with conn.cursor() as cur:
        for i in range(0, len(data_seq), chunk_size):
            batch = data_seq[i : i + chunk_size]
            cur.executemany(stmt, batch)
            total += len(batch)
        conn.commit()

    LOG.info(
        "bulk_upsert: table=%s rows=%d conflict=%s update=%s",
        table, total, ",".join(conflict_cols), ",".join(update_cols) if update_cols else "(none)"
    )
    return total

# -----------------------------------------------------------------------------#
# Internals
# -----------------------------------------------------------------------------#

def _ident_table(table: str) -> sql.Composed:
    if "." in table:
        schema, name = table.split(".", 1)
        return sql.SQL(".").join([sql.Identifier(schema), sql.Identifier(name)])
    return sql.Identifier(table)

def _primary_key_cols(conn: psycopg.Connection, table: str) -> List[str]:
    if "." in table:
        schema, name = table.split(".", 1)
    else:
        schema, name = "public", table
    q = """
    SELECT a.attname
    FROM pg_index i
    JOIN pg_class c ON c.oid = i.indrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
    WHERE n.nspname = %s AND c.relname = %s AND i.indisprimary
    ORDER BY a.attnum;
    """
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(q, (schema, name))
        rows = cur.fetchall()
    return [r[0] for r in rows]
