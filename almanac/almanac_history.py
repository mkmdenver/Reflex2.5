"""
almanac_history.py

Historical Atmosphere Almanac generator (LONG FORM into breadth_1m).

Runs like backfill:
- target: ALL | symbol | wildcard (K*)
- date range inclusive
- mode:
    overwrite : delete target range rows for the relevant scopes, then regenerate
    append    : upsert only (safe for extending forward)

Env: uses repo .env (REFLEX_PG_DSN / PG*).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from atmosphere_core import (
    AlmanacConfig,
    build_fixed_baseline,
    connect_pg,
    daterange,
    day_bounds_utc,
    delete_breadth_range,
    fetch_symbols_for_range,
    find_repo_root,
    generate_day,
    load_dotenv,
    parse_ymd,
    select_symbols,
)


def usage() -> None:
    print(
        "Usage:\n"
        "  python almanac_history.py TARGET START_DATE [END_DATE]\n"
        "    [--lookback N]\n"
        "    [--universe NAME]\n"
        "    [--run-id ID]\n"
        "    [--mode overwrite|append]\n"
        "    [--clear scope_prefix]\n"
        "    [--no-spy]\n"
        "\n"
        "Examples:\n"
        "  python almanac_history.py ALL  2021-01-01 2021-01-31 --mode overwrite\n"
        "  python almanac_history.py KROS 2025-08-01 2025-12-05 --mode overwrite\n"
        "  python almanac_history.py K*  2025-11-01 2025-11-30 --mode append\n"
        "  python almanac_history.py ALL 2025-11-01 2025-11-30 --clear universe:ALL\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if len(argv) < 2:
        usage()
        return 1

    target = argv[0]
    start_date = parse_ymd(argv[1])

    # END_DATE optional
    idx = 2
    end_date = start_date
    if idx < len(argv) and not argv[idx].startswith("--"):
        end_date = parse_ymd(argv[idx])
        idx += 1

    lookback = 30
    universe_name = "ALL"
    run_id = datetime.now().strftime("hist_%Y%m%d_%H%M%S")
    mode = "overwrite"
    clear_prefix: Optional[str] = None
    write_spy = True

    while idx < len(argv):
        a = argv[idx]
        if a == "--lookback" and idx + 1 < len(argv):
            lookback = int(argv[idx + 1]); idx += 2; continue
        if a == "--universe" and idx + 1 < len(argv):
            universe_name = argv[idx + 1]; idx += 2; continue
        if a == "--run-id" and idx + 1 < len(argv):
            run_id = argv[idx + 1]; idx += 2; continue
        if a == "--mode" and idx + 1 < len(argv):
            mode = argv[idx + 1].lower(); idx += 2; continue
        if a == "--clear" and idx + 1 < len(argv):
            clear_prefix = argv[idx + 1]; idx += 2; continue
        if a == "--no-spy":
            write_spy = False; idx += 1; continue
        idx += 1

    if mode not in ("overwrite", "append"):
        raise SystemExit(f"Invalid --mode '{mode}' (use overwrite|append)")

    # Load repo .env
    repo_root = find_repo_root(os.path.abspath(os.path.dirname(__file__)))
    load_dotenv(os.path.join(repo_root, ".env"))

    cfg = AlmanacConfig(
        target=target,
        start_date=start_date,
        end_date=end_date,
        lookback_days=lookback,
        universe_name=universe_name,
        write_spy=write_spy,
        run_id=run_id,
        mode=mode,
    )

    conn = connect_pg()

    # Build symbol list based on first day in range (fast + stable)
    d0s, d0e = day_bounds_utc(cfg.start_date)
    all_syms = fetch_symbols_for_range(conn, d0s, d0e)
    symbols_universe = select_symbols(all_syms, cfg.target)

    # For compute, optionally include SPY even if not in universe selection
    symbols_compute: List[str] = list(symbols_universe)
    if cfg.write_spy and ("SPY" in all_syms) and ("SPY" not in symbols_compute):
        symbols_compute.append("SPY")

    print(f"[RUN] target={cfg.target} range={cfg.start_date}..{cfg.end_date} mode={cfg.mode} run_id={cfg.run_id}")
    print(f"[SYMS] universe={len(symbols_universe)} compute={len(symbols_compute)} spy={('SPY' in symbols_compute)}")

    # Clear requested range/scopes
    if clear_prefix:
        # clear_prefix like 'universe:ALL' or 'etf:SPY'
        start_ts = datetime(cfg.start_date.year, cfg.start_date.month, cfg.start_date.day, tzinfo=timezone.utc)
        end_ts = datetime(cfg.end_date.year, cfg.end_date.month, cfg.end_date.day, tzinfo=timezone.utc) + (end_date - end_date + (end_date - end_date))  # noop, keep lint quiet

        # Real end_ts is end_date+1d
        end_ts = datetime(cfg.end_date.year, cfg.end_date.month, cfg.end_date.day, tzinfo=timezone.utc) + (datetime(cfg.end_date.year, cfg.end_date.month, cfg.end_date.day, tzinfo=timezone.utc) - datetime(cfg.end_date.year, cfg.end_date.month, cfg.end_date.day, tzinfo=timezone.utc))  # noop

        end_ts = datetime(cfg.end_date.year, cfg.end_date.month, cfg.end_date.day, tzinfo=timezone.utc) + (datetime(cfg.end_date.year, cfg.end_date.month, cfg.end_date.day, tzinfo=timezone.utc) - datetime(cfg.end_date.year, cfg.end_date.month, cfg.end_date.day, tzinfo=timezone.utc))  # noop

        # do it properly:
        end_ts = datetime(cfg.end_date.year, cfg.end_date.month, cfg.end_date.day, tzinfo=timezone.utc) + __import__("datetime").timedelta(days=1)

        n = delete_breadth_range(conn, start_ts, end_ts, scope_prefix=clear_prefix)
        print(f"[CLEAR] scope_prefix='{clear_prefix}' deleted_rows={n}")

    elif cfg.mode == "overwrite":
        # Overwrite canonical scopes we will write
        start_ts = datetime(cfg.start_date.year, cfg.start_date.month, cfg.start_date.day, tzinfo=timezone.utc)
        end_ts = datetime(cfg.end_date.year, cfg.end_date.month, cfg.end_date.day, tzinfo=timezone.utc) + __import__("datetime").timedelta(days=1)

        # universe scope always written
        uni_prefix = f"universe:{cfg.universe_name}"
        n1 = delete_breadth_range(conn, start_ts, end_ts, scope_prefix=uni_prefix)

        # etf:SPY written optionally
        n2 = 0
        if cfg.write_spy:
            n2 = delete_breadth_range(conn, start_ts, end_ts, scope_prefix="etf:SPY")

        print(f"[OVERWRITE] deleted universe_rows={n1} spy_rows={n2}")

    # Build baseline (fixed v1)
    baseline = build_fixed_baseline(conn, symbols_universe, cfg.start_date, cfg.lookback_days)
    print(f"[BASELINE] rows={len(baseline)} lookback_days={cfg.lookback_days}")

    # Generate day by day
    for d in daterange(cfg.start_date, cfg.end_date):
        print(f"[DAY] {d.isoformat()}")
        generate_day(conn, cfg, symbols_universe, symbols_compute, baseline, d)

    conn.close()
    print("[DONE] Almanac history generation complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
