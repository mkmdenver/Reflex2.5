"""
Reflex Tick Parquet Exporter
─────────────────────────────────────────────────────────────
Exports ticks from Postgres -> Parquet lake using your existing .env.

Respects your env exactly:

    REFLEX_PG_DSN              - Postgres DSN for stock_data
    REFLEX_TICKS_TABLE         - tick table name (defaults to "tick_data")
    REFLEX_TICK_PARQUET_ROOT   - preferred parquet root (optional)
    REFLEX_STORAGE_PARQUET_ROOT- fallback parquet root (optional)
    PARQUET_ROOT               - fallback parquet root (optional)

Simple directory layout:

    <PARQUET_ROOT>/<SYMBOL>/<SYMBOL>_YYYY-MM-DD.parquet

New QA feature:

    - For the date range [start, end), we infer "market days" as:
        all distinct dates that have *any* ticks in the table.
    - For each symbol that appears in that range, we check whether it
      has ticks on each market day.
    - Any (symbol, market_day) with no data is recorded as a coverage
      gap and reported at the end, but export does NOT stop.

Archive feature (prefix-based, KISS-safe):

    archive-prefix:
      - For each symbol under a prefix with ticks before cutoff:
          - For each day < cutoff:
              - db_count
              - export parquet (or verify if exists)
              - verify parquet row-count == db_count
              - optionally delete those rows (day partition)
          - optionally mark symbol_metadata.filters += 'tick_archived'

Note: end date / cutoff date is exclusive (range is [start, end)).
"""

import argparse
import datetime as dt
import os
import shutil
from pathlib import Path
from typing import Optional, List, Tuple, Dict

import psycopg
from psycopg.rows import dict_row
import pyarrow as pa
import pyarrow.parquet as pq
from dotenv import load_dotenv


# ============================================================
# ENV LOADER (fully respects your existing .env)
# ============================================================


def load_env() -> tuple[str, str, Path]:
    repo_root = Path(__file__).resolve().parents[1]
    env_path = repo_root / ".env"

    if env_path.exists():
        load_dotenv(env_path)

    dsn = os.getenv("REFLEX_PG_DSN")
    if not dsn:
        raise SystemExit("ERROR: REFLEX_PG_DSN missing from .env")

    tick_table = os.getenv("REFLEX_TICKS_TABLE", "tick_data")

    root = (
        os.getenv("REFLEX_TICK_PARQUET_ROOT")
        or os.getenv("REFLEX_STORAGE_PARQUET_ROOT")
        or os.getenv("PARQUET_ROOT")
    )

    if not root:
        raise SystemExit(
            "ERROR: No parquet root configured. Need one of:\n"
            "  REFLEX_TICK_PARQUET_ROOT\n"
            "  REFLEX_STORAGE_PARQUET_ROOT\n"
            "  PARQUET_ROOT\n"
        )

    parquet_root = Path(root)
    print(f"[ENV] DB=REFLEX_PG_DSN  TABLE={tick_table}  PARQUET_ROOT={parquet_root}")
    return dsn, tick_table, parquet_root


def parse_date(s: str) -> dt.date:
    return dt.datetime.strptime(s, "%Y-%m-%d").date()


def disk_free_bytes(path: Path) -> int:
    """Return free bytes on the filesystem containing `path`."""
    usage = shutil.disk_usage(str(path))
    return int(usage.free)


def fmt_gb(n_bytes: int) -> str:
    return f"{n_bytes / (1024**3):.2f} GB"


def has_table(conn: psycopg.Connection, table_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = %s
            LIMIT 1
            """,
            (table_name,),
        )
        return cur.fetchone() is not None


# ============================================================
# QUERY -> get (symbol, date) worklist
# ============================================================


def get_symbol_dates(
    conn: psycopg.Connection,
    table: str,
    start: dt.date,
    end_excl: dt.date,
    symbol: Optional[str] = None,
) -> List[Tuple[str, dt.date]]:
    """
    Return list of (symbol, trade_date) pairs that have tick rows
    in the tick table for the given date range.
    """
    start_ts = dt.datetime.combine(start, dt.time.min)
    end_ts = dt.datetime.combine(end_excl, dt.time.min)

    if symbol:
        print(
            f"[INFO] Scanning {table} for symbol={symbol} "
            f"between {start_ts} → {end_ts} ..."
        )
    else:
        print(
            f"[INFO] Scanning {table} for ALL symbols "
            f"between {start_ts} → {end_ts} ..."
        )

    with conn.cursor(row_factory=dict_row) as cur:
        if symbol:
            cur.execute(
                f"""
                SELECT symbol,
                       date("timestamp") AS d
                FROM {table}
                WHERE "timestamp" >= %s
                  AND "timestamp" <  %s
                  AND symbol = %s
                GROUP BY symbol, d
                ORDER BY symbol, d
                """,
                (start_ts, end_ts, symbol),
            )
        else:
            cur.execute(
                f"""
                SELECT symbol,
                       date("timestamp") AS d
                FROM {table}
                WHERE "timestamp" >= %s
                  AND "timestamp" <  %s
                GROUP BY symbol, d
                ORDER BY symbol, d
                """,
                (start_ts, end_ts),
            )
        rows = cur.fetchall()

    print(f"[INFO] Found {len(rows)} symbol/day partitions with data.")
    return [(r["symbol"], r["d"]) for r in rows]


# ============================================================
# WRITE ONE DAY → parquet   (simple layout)
# ============================================================


def export_one(
    conn: psycopg.Connection,
    table: str,
    root: Path,
    symbol: str,
    day: dt.date,
    overwrite: bool = False,
) -> None:
    """
    Export one (symbol, day) partition to:

        <root>/<symbol>/<symbol>_YYYY-MM-DD.parquet
    """
    out_dir = root / symbol
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{symbol}_{day}.parquet"

    if out_file.exists() and not overwrite:
        print(f"[SKIP] {symbol} {day} (file exists)")
        return

    day_start = dt.datetime.combine(day, dt.time.min)
    day_end = day_start + dt.timedelta(days=1)

    sql = f"""
    SELECT
        symbol,
        "timestamp",
        sip_timestamp,
        participant_timestamp,
        trf_timestamp,
        price,
        size,
        exchange,
        conditions,
        tape,
        trade_id
    FROM {table}
    WHERE symbol = %s
      AND "timestamp" >= %s
      AND "timestamp" <  %s
    ORDER BY "timestamp", trade_id
    """

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (symbol, day_start, day_end))
        rows = cur.fetchall()

    if not rows:
        print(f"[EMPTY] {symbol} {day} (no rows in {table})")
        return

    tbl = pa.Table.from_pydict(
        {
            "symbol": [r["symbol"] for r in rows],
            "timestamp": [r["timestamp"] for r in rows],
            "sip_ts": [r["sip_timestamp"] for r in rows],
            "part_ts": [r["participant_timestamp"] for r in rows],
            "trf_ts": [r["trf_timestamp"] for r in rows],
            "price": [float(r["price"]) for r in rows],
            "size": [r["size"] for r in rows],
            "exchange": [r["exchange"] for r in rows],
            "conditions": [r["conditions"] for r in rows],
            "tape": [r["tape"] for r in rows],
            "trade_id": [r["trade_id"] for r in rows],
        }
    )

    pq.write_table(tbl, out_file)
    print(f"[WRITE] {symbol} {day} → {out_file}  ({tbl.num_rows} rows)")


def parquet_rows(path: Path) -> int:
    """Fast row-count read from Parquet metadata."""
    pf = pq.ParquetFile(path)
    return int(pf.metadata.num_rows)


def count_rows_for_day(
    conn: psycopg.Connection,
    table: str,
    symbol: str,
    day: dt.date,
) -> int:
    day_start = dt.datetime.combine(day, dt.time.min)
    day_end = day_start + dt.timedelta(days=1)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM {table}
            WHERE symbol = %s
              AND "timestamp" >= %s
              AND "timestamp" <  %s
            """,
            (symbol, day_start, day_end),
        )
        return int(cur.fetchone()[0])


def delete_rows_for_day(
    conn: psycopg.Connection,
    table: str,
    symbol: str,
    day: dt.date,
) -> int:
    """Delete a single day partition (committed immediately)."""
    day_start = dt.datetime.combine(day, dt.time.min)
    day_end = day_start + dt.timedelta(days=1)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            DELETE FROM {table}
            WHERE symbol = %s
              AND "timestamp" >= %s
              AND "timestamp" <  %s
            """,
            (symbol, day_start, day_end),
        )
        deleted = cur.rowcount
    conn.commit()
    return int(deleted)


def symbol_is_tick_archived(conn: psycopg.Connection, symbol: str) -> bool:
    """Best-effort check based on symbol_metadata.filters containing 'tick_archived'."""
    if not has_table(conn, "symbol_metadata"):
        return False
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM symbol_metadata
            WHERE symbol = %s
              AND filters @> ARRAY['tick_archived']::text[]
            LIMIT 1
            """,
            (symbol,),
        )
        return cur.fetchone() is not None


def mark_symbol_tick_archived(conn: psycopg.Connection, symbol: str) -> None:
    """Mark symbol as tick-archived so backfill can skip re-downloading."""
    if not has_table(conn, "symbol_metadata"):
        print(
            "[WARN] symbol_metadata table not found; cannot mark tick_archived. "
            "(Upload the current schema if this DB uses a different table.)"
        )
        return
    with conn.cursor() as cur:
        # Ensure row exists (idempotent)
        cur.execute(
            """
            INSERT INTO symbol_metadata(symbol, filters)
            VALUES (%s, ARRAY[]::text[])
            ON CONFLICT (symbol) DO NOTHING
            """,
            (symbol,),
        )
        # Add the flag if missing
        cur.execute(
            """
            UPDATE symbol_metadata
            SET filters = CASE
                WHEN filters @> ARRAY['tick_archived']::text[] THEN filters
                ELSE array_append(filters, 'tick_archived')
            END,
            last_updated = NOW()
            WHERE symbol = %s
            """,
            (symbol,),
        )
    conn.commit()
    print(f"[META] Marked {symbol} as tick_archived")


def get_symbols_with_ticks_before(
    conn: psycopg.Connection,
    table: str,
    cutoff_excl: dt.date,
    prefix: Optional[str] = None,
    include_archived: bool = False,
) -> List[str]:
    """Return symbols that have tick rows strictly before cutoff_excl.

    IMPORTANT: Avoid the SQL anti-pattern `(%s IS NULL OR col LIKE %s)`.
    Postgres can throw IndeterminateDatatype on the NULL-typed parameter.
    Build the WHERE clause conditionally instead.
    """

    cutoff_ts = dt.datetime.combine(cutoff_excl, dt.time.min)
    prefix_like = f"{prefix.upper()}%" if prefix else None

    # Note: symbol_metadata may not exist in older DBs; in that case we can't exclude.
    can_exclude = (not include_archived) and has_table(conn, "symbol_metadata")

    where_parts = ['t."timestamp" < %s']
    params: List[object] = [cutoff_ts]

    if prefix_like:
        where_parts.append('t.symbol LIKE %s')
        params.append(prefix_like)

    if can_exclude:
        where_parts.append(
            "NOT (COALESCE(m.filters, ARRAY[]::text[]) @> ARRAY['tick_archived']::text[])"
        )

    where_sql = " AND ".join(where_parts)

    if can_exclude:
        sql = f"""
            SELECT DISTINCT t.symbol
            FROM {table} t
            LEFT JOIN symbol_metadata m ON m.symbol = t.symbol
            WHERE {where_sql}
            ORDER BY t.symbol
        """
    else:
        # Keep alias `t` so the same where_sql works in both branches.
        sql = f"""
            SELECT DISTINCT t.symbol
            FROM {table} t
            WHERE {where_sql}
            ORDER BY t.symbol
        """

    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()

    return [r[0] for r in rows]


def get_symbol_days_before(
    conn: psycopg.Connection,
    table: str,
    symbol: str,
    cutoff_excl: dt.date,
) -> List[dt.date]:
    cutoff_ts = dt.datetime.combine(cutoff_excl, dt.time.min)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT date("timestamp") AS d
            FROM {table}
            WHERE symbol = %s
              AND "timestamp" < %s
            GROUP BY d
            ORDER BY d
            """,
            (symbol, cutoff_ts),
        )
        rows = cur.fetchall()
    return [r["d"] for r in rows]


# ============================================================
# MARKET-DAY COVERAGE QA
# ============================================================


def compute_market_day_gaps(
    jobs: List[Tuple[str, dt.date]]
) -> Dict[str, List[dt.date]]:
    """
    Given a list of (symbol, date) that HAVE data, compute for each symbol
    the set of "market days" where that symbol has NO data.

    Market days are defined as the union of all dates that have ticks for
    ANY symbol in the given range. That automatically excludes weekends/
    holidays where the market is fully closed in your DB world.

    Returns:
        gaps_by_symbol: { symbol -> [missing_date1, missing_date2, ...] }
    """
    if not jobs:
        return {}

    existing = set(jobs)
    symbols = sorted({sym for sym, _ in jobs})
    market_days = sorted({d for _, d in jobs})

    gaps: Dict[str, List[dt.date]] = {}

    for sym in symbols:
        missing_days: List[dt.date] = []
        for d in market_days:
            if (sym, d) not in existing:
                missing_days.append(d)

        if missing_days:
            gaps[sym] = missing_days

    return gaps


def print_gap_summary(
    gaps: Dict[str, List[dt.date]],
    start: dt.date,
    end_excl: dt.date,
) -> None:
    """
    Print a summary of coverage gaps at the bottom of the run.
    This does NOT stop the export; it's purely QA reporting.
    """
    if not gaps:
        print("\n[QA] Market-day coverage: no gaps detected in this range.")
        return

    total_gaps = sum(len(v) for v in gaps.values())
    print("\n[ERROR] Market-day coverage gaps detected (export NOT stopped).")
    print(
        f"[ERROR] A 'market day' is any date where at least one symbol "
        f"had ticks in [{start} → {end_excl})."
    )
    print(f"[ERROR] Total missing (symbol, market_day) pairs: {total_gaps}")
    print(f"[ERROR] Symbols with gaps: {len(gaps)}\n")

    # Summarize per symbol (top 40 worst offenders)
    items = sorted(gaps.items(), key=lambda kv: len(kv[1]), reverse=True)
    print("[ERROR] Top symbols by missing market days:")
    for sym, days in items[:40]:
        print(f"    {sym}: {len(days)} missing days")

    # Show a small sample of specific gaps (not all, to avoid huge logs)
    print("\n[ERROR] Sample of specific gaps (up to 40 entries):")
    count = 0
    for sym, days in items:
        for d in days:
            print(f"    MISSING: symbol={sym}  day={d.isoformat()}")
            count += 1
            if count >= 40:
                break
        if count >= 40:
            break

    print(
        "\n[ERROR] NOTE: Export completed; this is a QA report. "
        "Use this to drive S3 / REST backfills or further investigation."
    )


# ============================================================
# RANGE EXPORT
# ============================================================


def export_range(
    dsn: str,
    table: str,
    root: Path,
    start: dt.date,
    end_excl: dt.date,
    symbol: Optional[str] = None,
    overwrite: bool = False,
) -> None:
    print("[INFO] Connecting to Postgres...")
    with psycopg.connect(dsn) as conn:
        jobs = get_symbol_dates(conn, table, start, end_excl, symbol)

        if not jobs:
            print("[INFO] No symbol/day partitions with data in this range.")
            print_gap_summary({}, start, end_excl)
            return

        # Compute coverage gaps BEFORE export (market-day QA)
        gaps = compute_market_day_gaps(jobs)

        total = len(jobs)
        print(f"[RUN] Exporting {total} partitions → {root}\n")

        for idx, (sym, day) in enumerate(jobs, start=1):
            print(f"[{idx}/{total}] {sym} {day}")
            export_one(conn, table, root, sym, day, overwrite)

        print("\n[COMPLETE] Tick Parquet Export Finished ✔")

        # Print coverage QA summary at the end so it doesn't scroll away
        print_gap_summary(gaps, start, end_excl)


# ============================================================
# ARCHIVE FLOW (export -> verify -> delete -> mark)
# ============================================================


def ensure_min_free_space(root: Path, min_free_gb: float) -> None:
    free = disk_free_bytes(root)
    need = int(min_free_gb * (1024**3))
    if free < need:
        raise SystemExit(
            f"ERROR: Not enough free space on {root}. "
            f"Free={fmt_gb(free)}  Required>={min_free_gb:.2f} GB"
        )
    print(f"[DISK] {root} free={fmt_gb(free)}  (min {min_free_gb:.2f} GB)")


def archive_symbol_before(
    conn: psycopg.Connection,
    table: str,
    root: Path,
    symbol: str,
    cutoff_excl: dt.date,
    overwrite: bool,
    delete_after: bool,
    dry_run: bool,
) -> None:
    days = get_symbol_days_before(conn, table, symbol, cutoff_excl)
    if not days:
        print(f"[SKIP] {symbol}: no ticks before {cutoff_excl}")
        return

    out_dir = root / symbol
    out_dir.mkdir(parents=True, exist_ok=True)

    total_days = len(days)
    print(f"[SYMBOL] {symbol}: {total_days} day partitions to archive (< {cutoff_excl})")

    for i, day in enumerate(days, start=1):
        out_file = out_dir / f"{symbol}_{day}.parquet"
        db_n = count_rows_for_day(conn, table, symbol, day)
        if db_n == 0:
            print(f"  [{i}/{total_days}] {day}: [EMPTY] db has 0 rows (skipping)")
            continue

        print(f"  [{i}/{total_days}] {day}: db_rows={db_n}")

        if out_file.exists() and not overwrite:
            pq_n = parquet_rows(out_file)
            if pq_n != db_n:
                raise SystemExit(
                    f"ERROR: Existing parquet row mismatch for {symbol} {day}. "
                    f"parquet={pq_n} db={db_n} file={out_file}"
                )
            print(f"    [VERIFY] exists OK parquet_rows={pq_n}")
        else:
            if dry_run:
                print(f"    [DRY] would export -> {out_file}")
            else:
                export_one(conn, table, root, symbol, day, overwrite=overwrite)
                pq_n = parquet_rows(out_file)
                if pq_n != db_n:
                    raise SystemExit(
                        f"ERROR: Parquet row mismatch after export for {symbol} {day}. "
                        f"parquet={pq_n} db={db_n} file={out_file}"
                    )
                print(f"    [VERIFY] export OK parquet_rows={pq_n}")

        if delete_after:
            if dry_run:
                print(f"    [DRY] would delete {db_n} rows from {table} for {symbol} {day}")
            else:
                deleted = delete_rows_for_day(conn, table, symbol, day)
                print(f"    [DELETE] deleted_rows={deleted}")


def archive_prefix(
    dsn: str,
    table: str,
    root: Path,
    prefix: str,
    cutoff_excl: dt.date,
    overwrite: bool,
    delete_after: bool,
    mark_archived: bool,
    min_free_gb: float,
    dry_run: bool,
    include_already_archived: bool,
) -> None:
    prefix = prefix.upper()
    ensure_min_free_space(root, min_free_gb)

    print("[INFO] Connecting to Postgres...")
    with psycopg.connect(dsn) as conn:
        symbols = get_symbols_with_ticks_before(
            conn,
            table,
            cutoff_excl=cutoff_excl,
            prefix=prefix,
            include_archived=include_already_archived,
        )

        if not symbols:
            print(f"[INFO] No symbols with ticks before {cutoff_excl} for prefix '{prefix}'.")
            return

        print(
            f"[RUN] Archiving prefix '{prefix}'  symbols={len(symbols)}  "
            f"cutoff<{cutoff_excl}  delete_after={delete_after}  mark={mark_archived}  dry_run={dry_run}"
        )

        for idx, sym in enumerate(symbols, start=1):
            print(f"\n[{idx}/{len(symbols)}] === {sym} ===")

            if (not include_already_archived) and symbol_is_tick_archived(conn, sym):
                print(f"[SKIP] {sym} already tick_archived")
                continue

            ensure_min_free_space(root, min_free_gb)
            archive_symbol_before(
                conn=conn,
                table=table,
                root=root,
                symbol=sym,
                cutoff_excl=cutoff_excl,
                overwrite=overwrite,
                delete_after=delete_after,
                dry_run=dry_run,
            )

            if mark_archived:
                if dry_run:
                    print(f"[DRY] would mark {sym} tick_archived")
                else:
                    mark_symbol_tick_archived(conn, sym)

        print("\n[COMPLETE] Prefix archive finished ✔")


# ============================================================
# CLI ENTRY
# ============================================================


def main() -> None:
    dsn, table, root = load_env()

    ap = argparse.ArgumentParser(
        description=(
            "Export ticks from Postgres to Parquet (with QA), or archive ticks by prefix "
            "(export -> verify -> delete -> mark tick_archived)."
        )
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("export", help="export ticks over a date range")
    p.add_argument("--start", required=True, help="YYYY-MM-DD (inclusive)")
    p.add_argument("--end", required=True, help="YYYY-MM-DD (exclusive)")
    p.add_argument("--symbol", help="limit to a single symbol (optional)")
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="overwrite existing files (default: skip existing)",
    )

    a = sub.add_parser(
        "archive-prefix",
        help="archive ticks for symbols starting with a letter: export+verify then (optionally) delete and mark tick_archived",
    )
    a.add_argument("--prefix", required=True, help="symbol prefix letter (e.g., A)")
    a.add_argument(
        "--cutoff",
        default="2026-01-01",
        help="archive ticks strictly BEFORE this date (YYYY-MM-DD). Default: 2026-01-01",
    )
    a.add_argument(
        "--delete-after",
        action="store_true",
        help="after successful parquet verification, delete those ticks from Postgres",
    )
    a.add_argument(
        "--mark-archived",
        action="store_true",
        help="after processing each symbol, set symbol_metadata.filters += 'tick_archived'",
    )
    a.add_argument(
        "--min-free-gb",
        type=float,
        default=10.0,
        help="refuse to run if parquet root free space is below this (default: 10 GB)",
    )
    a.add_argument(
        "--overwrite",
        action="store_true",
        help="overwrite existing parquet files (default: verify existing and skip export)",
    )
    a.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would happen, but do not write or delete",
    )
    a.add_argument(
        "--include-already-archived",
        action="store_true",
        help="also process symbols already flagged tick_archived",
    )

    args = ap.parse_args()

    if args.cmd == "export":
        start = parse_date(args.start)
        end_excl = parse_date(args.end)
        if end_excl <= start:
            raise SystemExit("ERROR: end must be > start")

        export_range(
            dsn=dsn,
            table=table,
            root=root,
            start=start,
            end_excl=end_excl,
            symbol=args.symbol,
            overwrite=args.overwrite,
        )

    elif args.cmd == "archive-prefix":
        cutoff_excl = parse_date(args.cutoff)
        archive_prefix(
            dsn=dsn,
            table=table,
            root=root,
            prefix=args.prefix,
            cutoff_excl=cutoff_excl,
            overwrite=args.overwrite,
            delete_after=args.delete_after,
            mark_archived=args.mark_archived,
            min_free_gb=args.min_free_gb,
            dry_run=args.dry_run,
            include_already_archived=args.include_already_archived,
        )


if __name__ == "__main__":
    main()
