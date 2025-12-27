
# dbmanager/db_coverage.py  (Nov-03-2025)
# Summarize coverage and holes for daily_bars & minute_bars in TIMEDATA.
# - Per symbol: first_seen, last_seen, span_days, days_full/partial/missing, top gap ranges.
# - Datasets: daily_bars, minute_bars (390 bars = "full" weekday; 1..389 = "partial").
# - Defaults to start=2025-10-01 end=today, and all symbols present in each dataset in-range.
# Deps: psycopg[binary], python-dateutil

import argparse, os
from datetime import date, datetime, timedelta
from dateutil.parser import isoparse

try:
    import psycopg
except Exception:
    psycopg = None

TODAY = date.today()

def daterange(d0: date, d1: date):
    d = d0
    while d <= d1:
        yield d
        d += timedelta(days=1)

def is_weekday(d: date) -> bool:
    return d.weekday() < 5

def expected_for(dataset: str, day: date) -> int | None:
    if dataset == "minute_bars":
        return 390 if is_weekday(day) else 0
    if dataset == "daily_bars":
        return 1 if is_weekday(day) else 0
    return None

def fetch_symbols(conn, dataset: str, start: date, end: date, explicit: list[str] | None):
    if explicit:
        return [s.upper() for s in explicit]
    sql = {
        "daily_bars": """
            SELECT DISTINCT symbol
            FROM daily_bars
            WHERE timestamp >= %s::date AND timestamp < (%s::date + INTERVAL '1 day')
            ORDER BY 1
        """,
        "minute_bars": """
            SELECT DISTINCT symbol
            FROM minute_bars
            WHERE timestamp >= %s::date AND timestamp < (%s::date + INTERVAL '1 day')
            ORDER BY 1
        """,
    }[dataset]
    with conn.cursor() as cur:
        cur.execute(sql, (start.isoformat(), end.isoformat()))
        return [r[0] for r in cur.fetchall()]

def fetch_counts(conn, dataset: str, symbols: list[str], start: date, end: date):
    # returns dict: {symbol: {day: count}}
    out = {s: {} for s in symbols}
    if dataset == "minute_bars":
        sql = """
            SELECT symbol, date_trunc('day', timestamp)::date AS d, COUNT(*) AS c
            FROM minute_bars
            WHERE symbol = ANY(%s)
              AND timestamp >= %s::date
              AND timestamp < (%s::date + INTERVAL '1 day')
            GROUP BY 1,2
        """
    else:
        sql = """
            SELECT symbol, date_trunc('day', timestamp)::date AS d, COUNT(*) AS c
            FROM daily_bars
            WHERE symbol = ANY(%s)
              AND timestamp >= %s::date
              AND timestamp < (%s::date + INTERVAL '1 day')
            GROUP BY 1,2
        """
    with conn.cursor() as cur:
        cur.execute(sql, (symbols, start.isoformat(), end.isoformat()))
        for sym, d, c in cur.fetchall():
            out[sym][d] = int(c)
    return out

def gaps_for(days, exp_list, counts_map):
    """Return overall stats and up to three largest gap ranges."""
    first_seen = None
    last_seen  = None
    days_full = days_partial = days_missing = 0
    gaps = []
    in_gap = None  # (start_date, length)

    for d, e in zip(days, exp_list):
        v = counts_map.get(d, 0)
        # seen?
        if e is None:
            seen = v > 0
            full = seen
            partial = False
        elif e == 0:
            seen = True   # weekends/holidays not expected; treat as neutral
            full = True
            partial = False
        else:
            seen = v > 0
            full = v >= e
            partial = (0 < v < e)

        if e and e > 0:  # only score weekdays
            if full:   days_full += 1
            elif partial: days_partial += 1
            else:
                days_missing += 1
                if in_gap is None:
                    in_gap = [d, 1]
                else:
                    in_gap[1] += 1
            if seen and first_seen is None:
                first_seen = d
            if seen:
                last_seen = d

        # close gap when coverage returns or at end
        if in_gap and (full or partial):
            gaps.append(tuple(in_gap))
            in_gap = None

    if in_gap:
        gaps.append(tuple(in_gap))

    # sort gaps by length desc
    gaps.sort(key=lambda t: t[1], reverse=True)
    top3 = gaps[:3]
    span = (last_seen - first_seen).days + 1 if (first_seen and last_seen) else 0
    return {
        "first_seen": first_seen, "last_seen": last_seen, "span_days": span,
        "days_full": days_full, "days_partial": days_partial, "days_missing": days_missing,
        "top_gaps": top3
    }

def print_symbol_row(dataset, sym, stats):
    if not stats["first_seen"]:
        print(f"{sym:<8}  -- no data in window --")
        return
    gparts = []
    for g_start, g_len in stats["top_gaps"]:
        g_end = g_start + timedelta(days=g_len-1)
        gparts.append(f"{g_start}→{g_end}({g_len})")
    gaps_txt = ", ".join(gparts) if gparts else "-"
    print(f"{sym:<8}  {stats['first_seen']}  {stats['last_seen']}  {stats['span_days']:>4}d   "
          f"full:{stats['days_full']:>3}  partial:{stats['days_partial']:>3}  miss:{stats['days_missing']:>3}   "
          f"gaps: {gaps_txt}")

def run(dataset: str, pg_dsn: str, start_s: str, end_s: str, symbols_csv: str | None):
    if psycopg is None:
        raise SystemExit("psycopg is required. pip install psycopg[binary]")

    start = isoparse(start_s).date() if start_s else date(2025, 10, 1)
    end   = isoparse(end_s).date()   if end_s   else TODAY

    with psycopg.connect(pg_dsn) as conn:
        symbols = fetch_symbols(conn, dataset, start, end,
                                [s.strip().upper() for s in symbols_csv.split(",")] if symbols_csv else None)
        if not symbols:
            print(f"[{dataset}] No symbols found in-range {start}..{end}")
            return

        counts = fetch_counts(conn, dataset, symbols, start, end)
        days = list(daterange(start, end))
        exp = [expected_for(dataset, d) for d in days]

        print(f"\nDATASET: {dataset}  WINDOW: {start} → {end}  ({len(days)} days)")
        if dataset == "minute_bars":
            print("weekday expectation: 390 bars = full, 1..389 = partial; 0 = missing")
        else:
            print("weekday expectation: 1 row = full; 0 = missing")

        print(f"\n{'SYMBOL':<8}  FIRST_SEEN   LAST_SEEN    SPAN    COUNTS (full/partial/miss)   TOP GAPS")
        for sym in symbols:
            stats = gaps_for(days, exp, counts[sym])
            print_symbol_row(dataset, sym, stats)

def main():
    ap = argparse.ArgumentParser(description="DB coverage & gap scanner (daily_bars / minute_bars)")
    ap.add_argument("--pg-dsn", required=True)
    ap.add_argument("--dataset", required=True, choices=["daily_bars","minute_bars","both"])
    ap.add_argument("--start", help="YYYY-MM-DD (default 2025-10-01)")
    ap.add_argument("--end", help="YYYY-MM-DD (default today)")
    ap.add_argument("--symbols", help="Comma-separated list; default = all symbols present in-range")
    args = ap.parse_args()

    if args.dataset == "both":
        run("daily_bars",  args.pg_dsn, args.start, args.end, args.symbols)
        run("minute_bars", args.pg_dsn, args.start, args.end, args.symbols)
    else:
        run(args.dataset, args.pg_dsn, args.start, args.end, args.symbols)

if __name__ == "__main__":
    main()
