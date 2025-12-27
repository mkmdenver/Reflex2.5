#!/usr/bin/env python3
"""
Reflex2.3 – Fill holes in minute + tick data between two dates.

This is a driver/orchestrator that:

1. Scans the DB for coverage between --since and --until
   for each symbol in public.symbol_metadata.
2. Determines which symbols are missing at least one *trading day*
   (Mon–Fri) for:
   - minute_bars
   - tick_data
3. For the missing symbols, calls the existing backfill tools:

   Minute:
     python -m tools.symbol_manager.db_backfill \
         --kind minute --symbol SYMBOL --since ... --until ...

   Ticks:
     python -m tools.symbol_manager.db_backfill_ticks \
         --symbol SYMBOL --since ... --until ...

Environment:
  LOG_LEVEL            – DEBUG/INFO/...
  REFLEX__PG_DSN       – primary Postgres DSN
  REFLEX_PG_DSN        – (alt) Postgres DSN
  PG_DSN / DATABASE_URL – fallback DSN names
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
from typing import Iterable, List, Sequence, Tuple

import psycopg


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


def _log(level: str, msg: str) -> None:
    now = dt.datetime.now().strftime("%H:%M:%S")
    print(f"{now} {level}:fill_holes:{msg}", flush=True)


def log_debug(msg: str) -> None:
    if LOG_LEVEL in ("DEBUG", "TRACE"):
        _log("DEBUG", msg)


def log_info(msg: str) -> None:
    _log("INFO", msg)


def log_warning(msg: str) -> None:
    _log("WARN", msg)


def log_error(msg: str) -> None:
    _log("ERROR", msg)


# ---------------------------------------------------------------------------
# DSN / dates / helpers
# ---------------------------------------------------------------------------

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


def parse_date(s: str) -> dt.date:
    try:
        return dt.date.fromisoformat(s)
    except Exception:
        raise SystemExit(f"Invalid date: {s!r} (expected YYYY-MM-DD)")


def market_days_between(since: dt.date, until: dt.date) -> List[dt.date]:
    """Return list of assumed trading days (Mon–Fri) in [since, until]."""
    days: List[dt.date] = []
    d = since
    while d <= until:
        if d.weekday() < 5:  # 0=Mon, 6=Sun
            days.append(d)
        d += dt.timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# DB coverage
# ---------------------------------------------------------------------------

def fetch_universe(conn: psycopg.Connection) -> List[str]:
    """All symbols from symbol_metadata, ordered."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol FROM public.symbol_metadata ORDER BY symbol;"
        )
        rows = cur.fetchall()
    syms = [r[0] for r in rows]
    log_info(f"Universe: {len(syms)} symbols from symbol_metadata")
    return syms


def count_distinct_days(
    conn: psycopg.Connection,
    table: str,
    ts_col: str,
    symbol: str,
    start_date: dt.date,
    end_date: dt.date,
) -> int:
    """
    Count distinct calendar days covered by [start_date, end_date] (UTC date).
    We don't need ET discipline for "at least one bar per calendar day".
    """
    start_ts = dt.datetime.combine(start_date, dt.time.min)
    end_ts = dt.datetime.combine(end_date + dt.timedelta(days=1), dt.time.min)

    sql = f"""
        SELECT COUNT(DISTINCT date_trunc('day', {ts_col}))::int
        FROM {table}
        WHERE symbol = %s
          AND {ts_col} >= %s
          AND {ts_col} < %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (symbol, start_ts, end_ts))
        (cnt,) = cur.fetchone()
    return int(cnt or 0)


def plan_missing_symbols(
    conn: psycopg.Connection,
    symbols: Sequence[str],
    since: dt.date,
    until: dt.date,
    need_minute: bool,
    need_ticks: bool,
) -> Tuple[List[str], List[str]]:
    """Return (missing_minute_symbols, missing_tick_symbols)."""
    market_days = market_days_between(since, until)
    needed_days = len(market_days)
    if needed_days == 0:
        log_warning("No trading days in window – nothing to do.")
        return [], []

    log_info(
        f"Using Mon–Fri weekdays as market days (needed_days={needed_days})"
    )

    missing_minute: List[str] = []
    missing_ticks: List[str] = []

    total = len(symbols)
    for idx, sym in enumerate(symbols, start=1):
        log_info(f"Scanning symbol {idx}/{total}: {sym}")
        # Minute
        if need_minute:
            cnt = count_distinct_days(
                conn,
                table="public.minute_bars",
                ts_col='"timestamp"',
                symbol=sym,
                start_date=since,
                end_date=until,
            )
            if cnt < needed_days:
                log_debug(
                    f"[{sym}] minute coverage missing: "
                    f"have_days={cnt} needed={needed_days} "
                    f"({since}→{until})"
                )
                missing_minute.append(sym)
            else:
                log_debug(f"[{sym}] minute coverage OK ({cnt}/{needed_days})")

        # Ticks
        if need_ticks:
            cnt = count_distinct_days(
                conn,
                table="public.tick_data",
                ts_col='"timestamp"',
                symbol=sym,
                start_date=since,
                end_date=until,
            )
            if cnt < needed_days:
                log_debug(
                    f"[{sym}] tick coverage missing: "
                    f"have_days={cnt} needed={needed_days} "
                    f"({since}→{until})"
                )
                missing_ticks.append(sym)
            else:
                log_debug(f"[{sym}] tick coverage OK ({cnt}/{needed_days})")

    return missing_minute, missing_ticks


# ---------------------------------------------------------------------------
# Backfill runners (call existing tools)
# ---------------------------------------------------------------------------

def run_cmd(cmd: Sequence[str]) -> int:
    log_info(f"[RUN] {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, check=False)
        if proc.returncode != 0:
            log_warning(f"[RUN] exit code {proc.returncode} for: {' '.join(cmd)}")
        return proc.returncode
    except KeyboardInterrupt:
        log_warning("Interrupted while running: " + " ".join(cmd))
        return 130
    except Exception as e:
        log_error(f"Error while running {' '.join(cmd)}: {e}")
        return 1


def backfill_minute_for_symbols(
    symbols: Iterable[str],
    since: dt.date,
    until: dt.date,
) -> None:
    """Call tools.symbol_manager.db_backfill (minute) per symbol."""
    symbols = list(symbols)
    if not symbols:
        return

    log_info(f"Minute backfill: {len(symbols)} symbol(s)")
    for i, sym in enumerate(symbols, start=1):
        log_info(f"[MINUTE] {sym} ({i}/{len(symbols)})")
        cmd = [
            sys.executable,
            "-m",
            "tools.symbol_manager.db_backfill",
            "--kind",
            "minute",
            "--symbol",
            sym,
            "--since",
            since.isoformat(),
            "--until",
            until.isoformat(),
        ]
        rc = run_cmd(cmd)
        if rc == 130:
            log_warning("Minute backfill interrupted – stopping further work.")
            break


def backfill_ticks_for_symbols(
    symbols: Iterable[str],
    since: dt.date,
    until: dt.date,
) -> None:
    """Call tools.symbol_manager.db_backfill_ticks per symbol."""
    symbols = list(symbols)
    if not symbols:
        return

    log_info(f"Tick backfill: {len(symbols)} symbol(s)")
    for i, sym in enumerate(symbols, start=1):
        log_info(f"[TICKS] {sym} ({i}/{len(symbols)})")
        cmd = [
            sys.executable,
            "-m",
            "tools.symbol_manager.db_backfill_ticks",
            "--symbol",
            sym,
            "--since",
            since.isoformat(),
            "--until",
            until.isoformat(),
        ]
        rc = run_cmd(cmd)
        if rc == 130:
            log_warning("Tick backfill interrupted – stopping further work.")
            break


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="db_fill_holes",
        description=(
            "Scan minute_bars + tick_data for coverage gaps between two dates, "
            "and call the existing backfill tools to fill missing symbols."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--since",
        required=True,
        help="Start date (YYYY-MM-DD, inclusive)",
    )
    parser.add_argument(
        "--until",
        required=True,
        help="End date (YYYY-MM-DD, inclusive)",
    )
    parser.add_argument(
        "--what",
        choices=["minute", "ticks", "both"],
        default="both",
        help="Which datasets to check/fill",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only: do not call backfill tools",
    )

    args = parser.parse_args(argv)

    since = parse_date(args.since)
    until = parse_date(args.until)
    if since > until:
        raise SystemExit("--since must be <= --until")

    do_minute = args.what in ("minute", "both")
    do_ticks = args.what in ("ticks", "both")

    log_info(
        f"Window: {since} → {until} what={args.what} dry_run={args.dry_run}"
    )

    dsn = detect_pg_dsn()
    log_info(f"Using Postgres DSN: {dsn}")

    # Connection + scan
    with psycopg.connect(dsn) as conn:
        symbols = fetch_universe(conn)

        missing_minute, missing_ticks = plan_missing_symbols(
            conn, symbols, since, until, do_minute, do_ticks
        )

    log_info(f"Symbols missing minute data: {len(missing_minute)}")
    log_info(f"Symbols missing tick data:   {len(missing_ticks)}")

    if args.dry_run:
        if do_minute and missing_minute:
            log_info("[DRY-RUN] Minute gaps for symbols: " + ", ".join(missing_minute))
        if do_ticks and missing_ticks:
            log_info("[DRY-RUN] Tick gaps for symbols:   " + ", ".join(missing_ticks))
        log_info("Dry-run complete; no backfill tools were called.")
        return 0

    # Actually call backfill tools
    if do_minute and missing_minute:
        backfill_minute_for_symbols(missing_minute, since, until)
    else:
        log_info("No symbols require minute backfill.")

    if do_ticks and missing_ticks:
        backfill_ticks_for_symbols(missing_ticks, since, until)
    else:
        log_info("No symbols require tick backfill.")

    log_info("Done.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log_warning("Interrupted by user.")
        raise
