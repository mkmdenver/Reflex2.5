"""
Reflex2 - Export tick_data to a Parquet tick lake.

Layout:

    <REFLEX__TICK_PARQUET_ROOT>/
        symbol=ADIL/
            date=2025-08-01/ticks.parquet
            date=2025-08-04/ticks.parquet
            ...
        symbol=SPY/
            date=2025-08-01/ticks.parquet
            ...

Environment (from repo_root/.env):

    REFLEX__PG_DSN              - psycopg3 DSN for your stock_data DB
    REFLEX__TICK_PARQUET_ROOT   - root folder for tick parquet lake

Usage examples (from repo root):

    python -m tools.tick_export_to_parquet export --start 2025-08-01 --end 2025-12-06

    # Single symbol:
    python -m tools.tick_export_to_parquet export --start 2025-08-01 --end 2025-12-06 --symbol SPY

Notes:

    - Dates are [start, end) — end is exclusive.
    - Existing ticks.parquet files are skipped unless --overwrite is given.
"""

import argparse
import datetime as dt
import os
from pathlib import Path
from typing import Optional, List, Tuple

import psycopg
from psycopg.rows import dict_row

import pyarrow as pa
import pyarrow.parquet as pq
from dotenv import load_dotenv


# ---------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------


def load_env() -> tuple[str, Path]:
    """
    Load .env from repo root (one level up from this file) and return
    (pg_dsn, parquet_root_path).
    """
    # This file is expected at <ROOT>/tools/tick_export_to_parquet.py
    repo_root = Path(__file__).resolve().parents[1]
    env_path = repo_root / ".env"

    if env_path.exists():
        load_dotenv(env_path)

    dsn = os.getenv("REFLEX__PG_DSN")
    if not dsn:
        raise SystemExit("REFLEX__PG_DSN not set in environment/.env")

    parquet_root_str = os.getenv("REFLEX__TICK_PARQUET_ROOT")
    if not parquet_root_str:
        raise SystemExit("REFLEX__TICK_PARQUET_ROOT not set in environment/.env")

    parquet_root = Path(parquet_root_str)
    return dsn, parquet_root


def parse_date(s: str) -> dt.date:
    try:
        return dt.datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"Invalid date {s!r}, expected YYYY-MM-DD") from exc


# ---------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------


def get_symbol_dates(
    conn: psycopg.Connection,
    start: dt.date,
    end_excl: dt.date,
    symbol: Optional[str],
) -> List[Tuple[str, dt.date]]:
    """
    Return list of (symbol, trade_date) pairs that actually have tick rows
    in public.tick_data for the given date range.

    This avoids iterating days that have no rows.
    """
    start_ts = dt.datetime.combine(start, dt.time.min)
    end_ts = dt.datetime.combine(end_excl, dt.time.min)

    with conn.cursor(row_factory=dict_row) as cur:
        if symbol:
            cur.execute(
                """
                SELECT symbol,
                       date("timestamp") AS trade_date
                FROM   public.tick_data
                WHERE  "timestamp" >= %s
                  AND  "timestamp" <  %s
                  AND  symbol = %s
                GROUP  BY symbol, date("timestamp")
                ORDER  BY symbol, trade_date
                """,
                (start_ts, end_ts, symbol),
            )
        else:
            cur.execute(
                """
                SELECT symbol,
                       date("timestamp") AS trade_date
                FROM   public.tick_data
                WHERE  "timestamp" >= %s
                  AND  "timestamp" <  %s
                GROUP  BY symbol, date("timestamp")
                ORDER  BY symbol, trade_date
                """,
                (start_ts, end_ts),
            )

        rows = cur.fetchall()

    return [(r["symbol"], r["trade_date"]) for r in rows]


# ---------------------------------------------------------------------
# Export logic
# ---------------------------------------------------------------------


def export_one_partition(
    conn: psycopg.Connection,
    parquet_root: Path,
    symbol: str,
    trade_date: dt.date,
    overwrite: bool = False,
) -> None:
    """
    Export one (symbol, trade_date) partition to:

        <parquet_root>/symbol=SYM/date=YYYY-MM-DD/ticks.parquet
    """
    day_start = dt.datetime.combine(trade_date, dt.time.min)
    day_end = day_start + dt.timedelta(days=1)

    out_dir = parquet_root / f"symbol={symbol}" / f"date={trade_date.isoformat()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "ticks.parquet"

    if out_path.exists() and not overwrite:
        print(f"[SKIP] {symbol} {trade_date} (already exists)")
        return

    sql = """
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
    FROM public.tick_data
    WHERE symbol = %s
      AND "timestamp" >= %s
      AND "timestamp" <  %s
    ORDER BY "timestamp", trade_id
    """

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (symbol, day_start, day_end))
        rows = cur.fetchall()

    if not rows:
        print(f"[EMPTY] {symbol} {trade_date} (no rows in tick_data)")
        return

    # Convert the result set into a PyArrow Table.
    data = {
        "symbol": [r["symbol"] for r in rows],
        "timestamp": [r["timestamp"] for r in rows],
        "sip_timestamp": [r["sip_timestamp"] for r in rows],
        "participant_timestamp": [r["participant_timestamp"] for r in rows],
        "trf_timestamp": [r["trf_timestamp"] for r in rows],
        "price": [float(r["price"]) if r["price"] is not None else None for r in rows],
        "size": [r["size"] for r in rows],
        "exchange": [r["exchange"] for r in rows],
        "conditions": [r["conditions"] for r in rows],
        "tape": [r["tape"] for r in rows],
        "trade_id": [r["trade_id"] for r in rows],
    }

    table = pa.Table.from_pydict(data)

    print(f"[WRITE] {symbol} {trade_date} -> {out_path} ({table.num_rows} rows)")
    pq.write_table(table, out_path)


def export_range(
    dsn: str,
    parquet_root: Path,
    start: dt.date,
    end_excl: dt.date,
    symbol: Optional[str],
    overwrite: bool,
) -> None:
    """
    Export ticks from public.tick_data into Parquet, for the given date range.
    """
    with psycopg.connect(dsn) as conn:
        jobs = get_symbol_dates(conn, start, end_excl, symbol)

        if not jobs:
            print(f"[INFO] No tick_data rows in range [{start}, {end_excl})")
            return

        print(
            f"[INFO] Exporting {len(jobs)} (symbol, date) partitions "
            f"in range [{start}, {end_excl}) to {parquet_root}"
        )

        for sym, td in jobs:
            export_one_partition(conn, parquet_root, sym, td, overwrite=overwrite)


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export public.tick_data to a Parquet tick lake"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_export = sub.add_parser("export", help="export ticks over a date range")
    p_export.add_argument(
        "--start",
        required=True,
        help="start date YYYY-MM-DD (inclusive)",
    )
    p_export.add_argument(
        "--end",
        required=True,
        help="end date YYYY-MM-DD (exclusive)",
    )
    p_export.add_argument(
        "--symbol",
        help="limit to a single symbol (optional)",
    )
    p_export.add_argument(
        "--overwrite",
        action="store_true",
        help="overwrite existing ticks.parquet files (default: skip existing)",
    )

    return parser


def main() -> None:
    dsn, parquet_root = load_env()
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.cmd == "export":
        start = parse_date(args.start)
        end_excl = parse_date(args.end)

        if end_excl <= start:
            raise SystemExit("end must be > start")

        export_range(
            dsn=dsn,
            parquet_root=parquet_root,
            start=start,
            end_excl=end_excl,
            symbol=args.symbol,
            overwrite=args.overwrite,
        )
    else:
        raise SystemExit(f"Unknown command {args.cmd}")


if __name__ == "__main__":
    main()
