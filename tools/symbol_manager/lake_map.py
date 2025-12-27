# dbmanager/lake_map.py
# Quick coverage map for your Parquet "lake" and/or DB.
# Dependencies: pyarrow>=12, python-dateutil, psycopg[binary] (only if using --pg-dsn)
# Usage examples:
#   python -m dbmanager.lake_map parquet --root "D:\Reflex\lake" --dataset minute_1 --start 2025-09-01 --end 2025-11-01 --symbols AAPL,TSLA --html report.html
#   python -m dbmanager.lake_map parquet --root "D:\Reflex\lake" --dataset ticks --start 2025-09-01 --end 2025-11-01 --symbols SPY --csv ticks_map.csv
#   python -m dbmanager.lake_map db --pg-dsn "%REFLEX__PG_DSN%" --dataset minute_1 --start 2025-09-01 --end 2025-11-01 --symbols AAPL,TSLA

import argparse, os, sys, glob, json, math
from datetime import date, datetime, timedelta
from dateutil.parser import isoparse
from dateutil.rrule import rrule, DAILY

# Optional imports (guarded)
try:
    import pyarrow as pa
    import pyarrow.dataset as ds
    import pyarrow.parquet as pq
except Exception:
    pa = ds = pq = None

try:
    import psycopg
except Exception:
    psycopg = None


BLOCKS = ["·", "░", "▒", "▓", "█"]  # ASCII intensity for coverage

def dtrange(start: date, end: date):
    for d in rrule(DAILY, dtstart=datetime.combine(start, datetime.min.time()), until=datetime.combine(end, datetime.min.time())):
        yield d.date()

def is_weekday(d: date) -> bool:
    return d.weekday() < 5  # simple: mon-fri; holidays handled by “expected=1/390 or unknown”

def expected_rows(dataset: str, day: date) -> int | None:
    # Simple expectations: minute_1 ~ 390 bars on full session weekdays, daily = 1 on weekdays; ticks/quotes unknown
    if dataset == "minute_1":
        return 390 if is_weekday(day) else 0
    if dataset == "daily":
        return 1 if is_weekday(day) else 0
    # ticks/quotes: can't predict; return None to mark "presence only"
    return None

def coverage_symbol_date_from_parquet(root: str, dataset: str, sym: str, day: date):
    """
    Scans partition like {root}/{dataset}/date=YYYY-MM-DD/symbol=SYM/*.parquet
    Returns (present: bool, rows: int)
    """
    day_str = day.isoformat()
    base = os.path.join(root, dataset, f"date={day_str}", f"symbol={sym}")
    if not os.path.isdir(base):
        return False, 0
    parts = glob.glob(os.path.join(base, "*.parquet"))
    if not parts:
        return False, 0
    # Sum row counts from metadata; no full read
    total = 0
    if pq is None:
        # Fallback if pyarrow not installed; assume presence == good, rows unknown
        return True, -1
    for p in parts:
        try:
            pf = pq.ParquetFile(p)
            total += pf.metadata.num_rows
        except Exception:
            # Corrupt/unfinished file: treat as present but unknown rows
            total = max(total, 0)
    return True, total

def intensity(value: float) -> str:
    idx = min(len(BLOCKS) - 1, max(0, int(round(value * (len(BLOCKS) - 1)))))
    return BLOCKS[idx]

def render_heat_row(dates, values, exp_values):
    cells = []
    for v, e in zip(values, exp_values):
        if e is None:
            # ticks/quotes: presence-only
            cells.append("█" if v and v > 0 else "·")
        else:
            if e == 0:
                cells.append(" ")  # weekend/holiday (expected 0)
            else:
                frac = max(0.0, min(1.0, (v or 0) / e))
                cells.append(intensity(frac))
    return "".join(cells)

def scan_parquet(root: str, dataset: str, symbols: list[str], start: date, end: date):
    days = list(dtrange(start, end))
    exp = [expected_rows(dataset, d) for d in days]
    out_rows = []
    for sym in symbols:
        vals = []
        for d in days:
            present, rows = coverage_symbol_date_from_parquet(root, dataset, sym, d)
            if rows < 0:  # unknown
                rows = 1 if present else 0
            vals.append(rows)
        out_rows.append((sym, vals))
    return days, exp, out_rows

def print_table(days, exp, rows, dataset, wide=False):
    # Header
    date_hdr = f"{days[0].isoformat()} → {days[-1].isoformat()} [{len(days)}d]"
    print(f"\nDATASET: {dataset}    WINDOW: {date_hdr}")
    print("Legend minute_1/daily: ·=0  ░≈25%  ▒≈50%  ▓≈75%  █=100%   (ticks/quotes: ·=absent, █=present)")
    if not wide:
        print("(Add --wide to show per-day characters; table below shows % coverage summary)\n")
    # Summary percent coverage per symbol
    print(f"{'SYMBOL':<8} {'COVERAGE%':>10} {'DAYS_OK':>8} {'DAYS_EXP>0':>12}")
    for sym, vals in rows:
        ok = 0
        exp_days = 0
        for v, e in zip(vals, exp):
            if e is None:
                # presence only
                if v > 0:
                    ok += 1
                    exp_days += 1
            else:
                if e > 0:
                    exp_days += 1
                    if v >= e:
                        ok += 1
        pct = 100.0 * ok / exp_days if exp_days else 0.0
        print(f"{sym:<8} {pct:10.1f} {ok:8d} {exp_days:12d}")
    if wide:
        print("\nHeatmap (one char per day):")
        day_labels = "".join("│" if (i % 5 == 4) else " " for i in range(len(days)))
        print(f"          {day_labels}")
        for sym, vals in rows:
            line = render_heat_row(days, vals, exp)
            print(f"{sym:<8} {line}")

def write_csv(path, days, exp, rows, dataset):
    import csv
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        hdr = ["symbol"] + [d.isoformat() for d in days]
        w.writerow(["dataset", dataset])
        w.writerow(hdr)
        for sym, vals in rows:
            w.writerow([sym] + vals)
    print(f"[csv] wrote {path}")

def write_html(path, days, exp, rows, dataset):
    # Minimal static HTML heatmap (no external deps)
    def cell(v, e):
        if e is None:
            color = "#1f77b4" if v and v > 0 else "#eee"
            title = "present" if v and v > 0 else "absent"
            return f'<td title="{title}" style="width:10px;height:10px;background:{color}"></td>'
        if e == 0:
            return '<td style="width:10px;height:10px;background:#fff"></td>'
        frac = max(0.0, min(1.0, (v or 0)/e))
        # grayscale 0..1
        shade = int(255 - frac*200)
        return f'<td title="{int(frac*100)}%" style="width:10px;height:10px;background:rgb({shade},{shade},{shade})"></td>'

    with open(path, "w", encoding="utf-8") as f:
        f.write("<html><head><meta charset='utf-8'><title>Lake Map</title></head><body>")
        f.write(f"<h3>Dataset: {dataset}</h3>")
        f.write(f"<p>Window: {days[0]} → {days[-1]} ({len(days)} days)</p>")
        f.write("<table border='0' cellspacing='1' cellpadding='0'>")
        # header
        f.write("<tr><th align='left'>Symbol</th>")
        for _ in days:
            f.write("<th></th>")
        f.write("</tr>")
        for sym, vals in rows:
            f.write(f"<tr><td style='padding-right:8px'>{sym}</td>")
            for v, e in zip(vals, exp):
                f.write(cell(v, e))
            f.write("</tr>")
        f.write("</table>")
        f.write("</body></html>")
    print(f"[html] wrote {path}")

def cmd_parquet(args):
    if ds is None or pq is None:
        print("pyarrow is required for parquet scanning. pip install pyarrow", file=sys.stderr)
        sys.exit(2)
    start = isoparse(args.start).date()
    end   = isoparse(args.end).date()
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else []
    if not symbols:
        print("Provide --symbols SYMBOL1,SYMBOL2 or point me at a file with --symbols-file", file=sys.stderr)
        sys.exit(2)
    days, exp, rows = scan_parquet(args.root, args.dataset, symbols, start, end)
    print_table(days, exp, rows, args.dataset, wide=args.wide)
    if args.csv:
        write_csv(args.csv, days, exp, rows, args.dataset)
    if args.html:
        write_html(args.html, days, exp, rows, args.dataset)

def db_fetch_coverage(conn, dataset, symbols, start, end):
    # Returns (days, exp, rows) similar to parquet path
    # Assumes minute_1 and daily schema from our plan.
    with conn.cursor() as cur:
        # Build date spine
        days = list(dtrange(start, end))
        exp = [expected_rows(dataset, d) for d in days]
        out = []
        for sym in symbols:
            if dataset == "minute_1":
                cur.execute("""
                    SELECT date_trunc('day',"timestamp")::date d, COUNT(*) c
                    FROM public.minute_1
                    WHERE symbol=%s AND "timestamp">=%s AND "timestamp"<%s
                    GROUP BY 1
                """, (sym, start, end + timedelta(days=1)))
                counts = {r[0]: r[1] for r in cur.fetchall()}
                vals = [counts.get(d, 0) for d in days]
            elif dataset == "daily":
                cur.execute("""
                    SELECT "timestamp"::date d, COUNT(*) c
                    FROM public.daily_bars
                    WHERE symbol=%s AND "timestamp">=%s AND "timestamp"<=%s
                    GROUP BY 1
                """, (sym, start, end))
                counts = {r[0]: r[1] for r in cur.fetchall()}
                vals = [counts.get(d, 0) for d in days]
            else:
                raise SystemExit(f"DB dataset not supported: {dataset}")
            out.append((sym, vals))
        return days, exp, out

def cmd_db(args):
    if psycopg is None:
        print("psycopg is required for DB coverage. pip install psycopg[binary]", file=sys.stderr)
        sys.exit(2)
    start = isoparse(args.start).date()
    end   = isoparse(args.end).date()
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else []
    if not symbols:
        print("Provide --symbols SYMBOL1,SYMBOL2 (DB mode needs explicit symbols).", file=sys.stderr)
        sys.exit(2)
    with psycopg.connect(args.pg_dsn) as conn:
        days, exp, rows = db_fetch_coverage(conn, args.dataset, symbols, start, end)
    print_table(days, exp, rows, args.dataset, wide=args.wide)
    if args.csv:
        write_csv(args.csv, days, exp, rows, args.dataset)
    if args.html:
        write_html(args.html, days, exp, rows, args.dataset)

def main():
    p = argparse.ArgumentParser(description="Reflex Lake Coverage Map")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("parquet", help="Scan Parquet partitions")
    sp.add_argument("--root", required=True, help="PARQUET_ROOT (e.g., D:\\Reflex\\lake)")
    sp.add_argument("--dataset", required=True, choices=["ticks","quotes","minute_1","daily"])
    sp.add_argument("--start", required=True)
    sp.add_argument("--end", required=True)
    sp.add_argument("--symbols", help="Comma-separated symbols to scan")
    sp.add_argument("--symbols-file", help="(optional) file with one symbol per line")
    sp.add_argument("--csv", help="write CSV here")
    sp.add_argument("--html", help="write heatmap HTML here")
    sp.add_argument("--wide", action="store_true", help="print ASCII heatmap in console")
    sp.set_defaults(func=cmd_parquet)

    sd = sub.add_parser("db", help="Scan TimescaleDB tables")
    sd.add_argument("--pg-dsn", required=True, help="postgresql://...")
    sd.add_argument("--dataset", required=True, choices=["minute_1","daily"])
    sd.add_argument("--start", required=True)
    sd.add_argument("--end", required=True)
    sd.add_argument("--symbols", required=True, help="Comma-separated symbols")
    sd.add_argument("--csv", help="write CSV here")
    sd.add_argument("--html", help="write heatmap HTML here")
    sd.add_argument("--wide", action="store_true", help="print ASCII heatmap in console")
    sd.set_defaults(func=cmd_db)

    args = p.parse_args()

    # Symbols file override if provided
    if getattr(args, "symbols_file", None):
        with open(args.symbols_file, "r", encoding="utf-8") as fh:
            syms = [ln.strip().upper() for ln in fh if ln.strip()]
        if getattr(args, "symbols", None):
            # unite both
            from itertools import chain
            args.symbols = ",".join(sorted(set(list(chain(syms, [s.strip().upper() for s in args.symbols.split(",")] )))))
        else:
            args.symbols = ",".join(syms)

    args.func(args)

if __name__ == "__main__":
    main()
