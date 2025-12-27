"""
almanac_bot.py

Realtime Almanac bot (STUB for now).
Lives in /almanac temporarily; later you can move it into the bot library.

Concept:
- Every minute, after the 1m bar closes, compute the same metrics and upsert to breadth_1m.
- Shares compute functions with almanac_history via atmosphere_core.py

For now:
- polls once per REFLEX_POLL_SECS (from .env) and processes the latest CLOSED minute for selected universe.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import List

import pandas as pd

from atmosphere_core import (
    AlmanacConfig,
    attach_rvol_self,
    build_fixed_baseline,
    connect_pg,
    fetch_symbols_for_range,
    find_repo_root,
    generate_day,
    load_dotenv,
    select_symbols,
)


def main() -> int:
    repo_root = find_repo_root(os.path.abspath(os.path.dirname(__file__)))
    load_dotenv(os.path.join(repo_root, ".env"))

    target = os.getenv("WATCH_SYMBOLS", "ALL")  # cheap default for now
    universe_name = os.getenv("REFLEX_INSTANCE_ID", "liveA")
    poll_secs = int(os.getenv("REFLEX_POLL_SECS", "30"))

    cfg = AlmanacConfig(
        target=target,
        start_date=datetime.now(timezone.utc).date(),
        end_date=datetime.now(timezone.utc).date(),
        lookback_days=30,
        universe_name=universe_name,
        write_spy=True,
        run_id=f"bot_{datetime.now().strftime('%Y%m%d')}",
        mode="append",
    )

    conn = connect_pg()

    # Universe symbols determined from "today so far"
    today = datetime.now(timezone.utc).date()
    d0 = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    d1 = d0 + timedelta(days=1)
    all_syms = fetch_symbols_for_range(conn, d0, d1)
    symbols_universe = select_symbols(all_syms, cfg.target)
    symbols_compute: List[str] = list(symbols_universe)
    if "SPY" in all_syms and "SPY" not in symbols_compute:
        symbols_compute.append("SPY")

    # Fixed baseline for the day (good enough for v1 bot)
    baseline = build_fixed_baseline(conn, symbols_universe, today, cfg.lookback_days)
    print(f"[BOT] universe={len(symbols_universe)} baseline_rows={len(baseline)} poll={poll_secs}s")

    while True:
        # process the last closed minute (UTC)
        now = datetime.now(timezone.utc)
        closed_minute = (now.replace(second=0, microsecond=0) - timedelta(minutes=1)).date()

        # for simplicity in v1: just run generate_day for today repeatedly;
        # the upserts make this idempotent, and it keeps logic shared.
        cfg.start_date = today
        cfg.end_date = today
        generate_day(conn, cfg, symbols_universe, symbols_compute, baseline, today)

        time.sleep(poll_secs)

    # conn.close() unreachable
    # return 0


if __name__ == "__main__":
    raise SystemExit(main())
