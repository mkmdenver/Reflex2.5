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

Usage examples (from repo root, via .bat or directly):

    python -m tools.tick_export_to_parquet export --start 2025-08-01 --end 2025-12-06
    python -m tools.tick_export_to_parquet export --symbol AAPL --start 2025-08-01 --end 2025-12-06

Note: end date is exclusive (range is [start, end)).
"""

import argparse
import datetime as dt
import os
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
# CLI ENTRY
# ============================================================


def main() -> None:
    dsn, table, root = load_env()

    ap = argparse.ArgumentParser(
        description="Export ticks from Postgres to Parquet and run coverage QA."
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


if __name__ == "__main__":
    main()
