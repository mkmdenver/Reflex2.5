import argparse
import datetime as dt
import os
import subprocess
import sys
from typing import Iterable, List, Optional

ENDPOINT_URL = "https://files.massive.com"
BUCKET = "s3://flatfiles"
DEFAULT_BASE_DIR = r"D:\polygon_flat\us_stocks_sip"

DATASETS = {
    "trades": "trades_v1",
    "quotes": "quotes_v1",
}

def _run(cmd: List[str]) -> int:
    # Print the command in a copy/paste friendly way
    print("[RUN]", " ".join(cmd))
    return subprocess.call(cmd)

def _parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)

def _daterange(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)

def _is_weekday(d: dt.date) -> bool:
    return d.weekday() < 5  # Mon=0..Fri=4

def _try_market_calendar_days(start: dt.date, end: dt.date) -> Optional[List[dt.date]]:
    """
    If pandas_market_calendars is installed, return actual NYSE sessions.
    Otherwise return None.
    """
    try:
        import pandas_market_calendars as mcal  # type: ignore
        import pandas as pd  # type: ignore
    except Exception:
        return None

    nyse = mcal.get_calendar("NYSE")
    sched = nyse.schedule(start_date=start.isoformat(), end_date=end.isoformat())
    if sched is None or len(sched) == 0:
        return []
    # schedule index is session dates as tz-aware timestamps
    sessions = [ts.date() for ts in sched.index.to_pydatetime()]
    return sessions

def _market_days(start: dt.date, end: dt.date, use_calendar: bool) -> List[dt.date]:
    if use_calendar:
        sessions = _try_market_calendar_days(start, end)
        if sessions is not None:
            return sessions
        print("[WARN] pandas_market_calendars not installed; falling back to weekdays-only.")

    return [d for d in _daterange(start, end) if _is_weekday(d)]

def _dst_path(base_dir: str, dataset_key: str, d: dt.date) -> str:
    # D:\polygon_flat\us_stocks_sip\trades_v1\2025\12\2025-12-31.csv.gz
    sub = DATASETS[dataset_key]
    return os.path.join(base_dir, sub, f"{d.year:04d}", f"{d.month:02d}", f"{d.isoformat()}.csv.gz")

def _s3_src(dataset_key: str, d: dt.date) -> str:
    # s3://flatfiles/us_stocks_sip/trades_v1/2025/12/2025-12-31.csv.gz
    sub = DATASETS[dataset_key]
    return f"{BUCKET}/us_stocks_sip/{sub}/{d.year:04d}/{d.month:02d}/{d.isoformat()}.csv.gz"

def _ensure_dir(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)

def _download_one(profile: str, dataset_key: str, d: dt.date, base_dir: str, overwrite: bool, quiet: bool) -> bool:
    src = _s3_src(dataset_key, d)
    dst = _dst_path(base_dir, dataset_key, d)
    _ensure_dir(dst)

    if (not overwrite) and os.path.exists(dst) and os.path.getsize(dst) > 0:
        print(f"[SKIP] {dataset_key} {d.isoformat()} already exists -> {dst}")
        return True

    cmd = [
        "aws", "s3", "cp",
        src, dst,
        "--profile", profile,
        "--endpoint-url", ENDPOINT_URL,
    ]
    if quiet:
        cmd.append("--no-progress")

    rc = _run(cmd)
    if rc != 0:
        print(f"[FAIL] {dataset_key} {d.isoformat()} (rc={rc})")
        return False

    print(f"[OK]   {dataset_key} {d.isoformat()} -> {dst}")
    return True

def main() -> int:
    ap = argparse.ArgumentParser(description="Download Massive/Polygon flat files (SIP trades/quotes) by market day.")
    ap.add_argument("--profile", default="massive", help="AWS CLI profile name (default: massive)")
    ap.add_argument("--base-dir", default=DEFAULT_BASE_DIR, help=f"Output base dir (default: {DEFAULT_BASE_DIR})")

    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--day", help="Single day YYYY-MM-DD")
    g.add_argument("--range", nargs=2, metavar=("START", "END"), help="Date range YYYY-MM-DD YYYY-MM-DD (inclusive)")

    ap.add_argument("--market-days", action="store_true", help="Use market days (weekdays or NYSE sessions)")
    ap.add_argument("--use-nyse-calendar", action="store_true",
                    help="Use NYSE calendar (requires pandas-market-calendars). If unavailable, falls back to weekdays.")
    ap.add_argument("--datasets", default="trades,quotes",
                    help="Comma list: trades,quotes (default: trades,quotes)")
    ap.add_argument("--overwrite", action="store_true", help="Overwrite existing files")
    ap.add_argument("--quiet", action="store_true", help="Suppress progress bar (--no-progress)")

    args = ap.parse_args()

    if args.day:
        start = end = _parse_date(args.day)
    else:
        start = _parse_date(args.range[0])
        end = _parse_date(args.range[1])
        if end < start:
            print("[ERROR] END must be >= START")
            return 2

    datasets = [s.strip() for s in args.datasets.split(",") if s.strip()]
    for ds in datasets:
        if ds not in DATASETS:
            print(f"[ERROR] Unknown dataset '{ds}'. Use trades,quotes.")
            return 2

    if args.market_days or args.use_nyse_calendar:
        days = _market_days(start, end, use_calendar=args.use_nyse_calendar)
    else:
        days = list(_daterange(start, end))

    if not days:
        print("[INFO] No days selected.")
        return 0

    ok_all = True
    for d in days:
        for ds in datasets:
            ok = _download_one(
                profile=args.profile,
                dataset_key=ds,
                d=d,
                base_dir=args.base_dir,
                overwrite=args.overwrite,
                quiet=args.quiet,
            )
            if not ok:
                ok_all = False

    return 0 if ok_all else 1

if __name__ == "__main__":
    raise SystemExit(main())
