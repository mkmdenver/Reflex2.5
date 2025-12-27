#!/usr/bin/env python
"""
Export ticks from Timescale (public.tick_data) to Parquet files, for replay.

Layout (by default):

    {PARQUET_ROOT}/ticks/YYYY/MM/DD/SYMBOL.parquet

Parquet root resolution (first non-empty wins):

    1) REFLEX_TICKS_PARQUET_ROOT
    2) REFLEX_STORAGE_PARQUET_ROOT
    3) PARQUET_ROOT
    4) ./parquet

Usage examples (from repo root):

    # Full universe export between two dates
    python -m tools.symbol_manager.export_ticks_to_parquet ^
            --since 2025-07-28 --until 2025-11-14

    # Single-symbol test run (nice for verifying layout / replay)
    python -m tools.symbol_manager.export_ticks_to_parquet ^
            --since 2025-10-01 --until 2025-10-10 --symbol ACCO

It streams by day using a server-side cursor so we don't pull an entire
day of ticks into RAM at once.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
from typing import Iterable, List, Optional, Tuple


def install_package(package: str) -> None:
        """Install a package using pip."""
        print(f"Installing {package}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])


try:
        import psycopg
        from psycopg.rows import tuple_row
except ImportError:
        install_package("psycopg[binary]")
        import psycopg
        from psycopg.rows import tuple_row

try:
        import pandas as pd
except ImportError:
        install_package("pandas")
        import pandas as pd



# ---------------------------------------------------------------------------
# Logging / env
# ---------------------------------------------------------------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


def log(level: str, msg: str) -> None:
    levels = ["DEBUG", "INFO", "WARN", "ERROR"]
    if levels.index(level) >= levels.index(LOG_LEVEL):
        ts = dt.datetime.now().strftime("%H:%M:%S")
        print(f"{ts} {level}:export_ticks:{msg}", flush=True)


def detect_pg_dsn() -> str:
    dsn = (
        os.getenv("REFLEX__PG_DSN")
        or os.getenv("REFLEX_PG_DSN")
        or os.getenv("PG_DSN")
        or os.getenv("DATABASE_URL")
    )
    if not dsn:
        raise SystemExit(
            "Set REFLEX__PG_DSN or REFLEX_PG_DSN or PG_DSN or DATABASE_URL "
            "for Postgres connection."
        )
    return dsn


PARQUET_ROOT = (
    os.getenv("REFLEX_TICKS_PARQUET_ROOT")
    or os.getenv("REFLEX_STORAGE_PARQUET_ROOT")
    or os.getenv("PARQUET_ROOT")
    or os.path.join(os.getcwd(), "parquet")
)


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def parse_date(s: str) -> dt.date:
    try:
        return dt.date.fromisoformat(s)
    except Exception:
        raise argparse.ArgumentTypeError(f"Invalid date {s!r}, expected YYYY-MM-DD")


def daterange(since: dt.date, until: dt.date) -> Iterable[dt.date]:
    d = since
    while d <= until:
        yield d
        d += dt.timedelta(days=1)


# ---------------------------------------------------------------------------
# Symbol + parquet helpers
# ---------------------------------------------------------------------------

def load_universe(conn: psycopg.Connection) -> List[str]:
    """
    Universe for info/logging: we default to symbol_metadata.
    Export itself doesn't need the full list unless you want to
    reason about which symbols "should" have ticks.
    """
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute("SELECT symbol FROM public.symbol_metadata ORDER BY symbol;")
        rows = cur.fetchall()
    syms = [r[0] for r in rows]
    log("INFO", f"Universe: {len(syms)} symbols from symbol_metadata")
    return syms


def parquet_path_for(symbol: str, day: dt.date) -> str:
    """
    Map (symbol, day) -> Parquet file path:

      {PARQUET_ROOT}/ticks/YYYY/MM/DD/SYMBOL.parquet
    """
    root = PARQUET_ROOT
    subdir = os.path.join(
        root,
        "ticks",
        f"{day.year:04d}",
        f"{day.month:02d}",
        f"{day.day:02d}",
    )
    os.makedirs(subdir, exist_ok=True)
    return os.path.join(subdir, f"{symbol}.parquet")


def merge_into_parquet(symbol: str, day: dt.date, rows: List[Tuple]) -> None:
    """
    Given a list of tick rows for (symbol, day), merge into the corresponding
    Parquet file, de-duplicating on (timestamp, trade_id).
    """
    if not rows:
        return

    cols = [
        "symbol",
        "timestamp",
        "sip_timestamp",
        "participant_timestamp",
        "trf_timestamp",
        "price",
        "size",
        "exchange",
        "conditions",
        "tape",
        "trade_id",
    ]
    df_new = pd.DataFrame(rows, columns=cols)

    path = parquet_path_for(symbol, day)

    if os.path.exists(path):
        try:
            df_old = pd.read_parquet(path)
            df = pd.concat([df_old, df_new], ignore_index=True)
            df.drop_duplicates(subset=["timestamp", "trade_id"], inplace=True)
            df.sort_values("timestamp", inplace=True)
        except Exception as e:
            log("WARN", f"[{symbol}] failed to merge with existing {path}; overwriting. err={e}")
            df = df_new
    else:
        df = df_new

    df.to_parquet(path, index=False)
    log("DEBUG", f"[{symbol}] wrote {len(df_new)} new rows to {path}")


# ---------------------------------------------------------------------------
# Core export loop
# ---------------------------------------------------------------------------

# Base query for a whole day (all symbols)
DAY_QUERY_ALL = """
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
WHERE "timestamp" >= %s
  AND "timestamp" <  %s
ORDER BY symbol, "timestamp";
"""

# Variant when we only want a single symbol
DAY_QUERY_ONE = """
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
WHERE "timestamp" >= %s
  AND "timestamp" <  %s
  AND symbol = %s
ORDER BY symbol, "timestamp";
"""


def export_day(
    conn: psycopg.Connection,
    day: dt.date,
    symbol: Optional[str] = None,
    fetch_size: int = 50_000,
) -> None:
    """
    Export one calendar day of ticks to Parquet.

    - If symbol is None: export all symbols with ticks that day.
    - If symbol is set: export only that symbol.

    Uses a server-side cursor to avoid loading the entire day into RAM.
    """
    start_ts = dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc)
    end_ts = start_ts + dt.timedelta(days=1)

    if symbol:
        log("INFO", f"[{day}] exporting ticks for {symbol} → parquet (root={PARQUET_ROOT})")
        sql = DAY_QUERY_ONE
        params = (start_ts, end_ts, symbol)
        cur_name = f"ticks_export_{symbol}_{day.isoformat()}"
    else:
        log("INFO", f"[{day}] exporting ticks for ALL symbols → parquet (root={PARQUET_ROOT})")
        sql = DAY_QUERY_ALL
        params = (start_ts, end_ts)
        cur_name = f"ticks_export_{day.isoformat()}"

    # Named server-side cursor
    with conn.cursor(name=cur_name, row_factory=tuple_row) as cur:
        cur.execute(sql, params)

        current_symbol: Optional[str] = None
        current_rows: List[Tuple] = []
        total_rows = 0

        while True:
            batch = cur.fetchmany(fetch_size)
            if not batch:
                break

            for row in batch:
                row_symbol = row[0]

                if current_symbol is None:
                    current_symbol = row_symbol

                if row_symbol != current_symbol:
                    # Flush previous symbol to Parquet
                    merge_into_parquet(current_symbol, day, current_rows)
                    total_rows += len(current_rows)
                    current_rows = []
                    current_symbol = row_symbol

                current_rows.append(row)

        # Flush last symbol for the day
        if current_symbol is not None and current_rows:
            merge_into_parquet(current_symbol, day, current_rows)
            total_rows += len(current_rows)

        if symbol:
            log("INFO", f"[{day}] done: exported {total_rows} tick rows for {symbol}")
        else:
            log("INFO", f"[{day}] done: exported {total_rows} tick rows (all symbols)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="export_ticks_to_parquet",
        description=(
            "Export ticks from public.tick_data to Parquet files for replay.\n"
            f"Parquet root: {PARQUET_ROOT}"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--since",
        required=True,
        type=parse_date,
        help="Start date (YYYY-MM-DD, inclusive)",
    )
    parser.add_argument(
        "--until",
        required=True,
        type=parse_date,
        help="End date (YYYY-MM-DD, inclusive)",
    )
    parser.add_argument(
        "--symbol",
        help=(
            "Limit export to a single symbol (for testing or partial fills). "
            "If omitted, all symbols with ticks in the window are exported."
        ),
    )
    parser.add_argument(
        "--fetch-size",
        type=int,
        default=50_000,
        help="Number of rows per fetch from server-side cursor",
    )

    args = parser.parse_args(argv)

    if args.until < args.since:
        parser.error("--until must be >= --since")

    dsn = detect_pg_dsn()
    log("INFO", f"Using Postgres DSN: {dsn}")
    log("INFO", f"Parquet root: {PARQUET_ROOT}")
    log("INFO", f"Window: {args.since} → {args.until}")

    symbol = args.symbol.upper() if args.symbol else None
    if symbol:
        log("INFO", f"Single-symbol mode: {symbol}")
    else:
        log("INFO", "Universe mode: exporting all symbols with ticks in this window")

    with psycopg.connect(dsn) as conn:
        if not symbol:
            # Only for logging / sanity; export_day doesn't need the universe
            universe = load_universe(conn)
            log("INFO", f"(Reference) universe size: {len(universe)} symbols")

        for day in daterange(args.since, args.until):
            export_day(conn, day, symbol=symbol, fetch_size=args.fetch_size)

    log("INFO", "Export complete.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("WARN", "Interrupted by user; partial export may exist on disk.")
        raise
