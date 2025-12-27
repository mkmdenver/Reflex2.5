#!/usr/bin/env python
"""
Quick Polygon /v3/trades debugger.

Usage:
    python debug_polygon_trades.py ASNS 2025-11-21

Reads POLYGON_API_KEY from environment, calls Polygon,
and dumps the raw response text so we can see what's going on.
"""

import os
import sys
import datetime as dt
import requests

def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    if len(argv) != 2:
        print("Usage: debug_polygon_trades.py SYMBOL YYYY-MM-DD", file=sys.stderr)
        return 1

    symbol = argv[0].upper()
    date_str = argv[1]

    try:
        day = dt.date.fromisoformat(date_str)
    except Exception as e:
        print(f"Bad date {date_str!r}, expected YYYY-MM-DD: {e}", file=sys.stderr)
        return 1

    api_key = os.getenv("POLYGON_API_KEY")
    if not api_key:
        print("POLYGON_API_KEY not set in environment", file=sys.stderr)
        return 1

    utc = dt.timezone.utc
    day_start = dt.datetime(day.year, day.month, day.day, tzinfo=utc)
    day_end   = day_start + dt.timedelta(days=1)

    gte_ns = int(day_start.timestamp() * 1_000_000_000)
    lt_ns  = int(day_end.timestamp()   * 1_000_000_000)

    url = f"https://api.polygon.io/v3/trades/{symbol}"
    params = {
        "timestamp.gte": gte_ns,
        "timestamp.lt": lt_ns,
        "order": "asc",
        "limit": 100,      # just grab a small sample
        "sort": "timestamp",
        "apiKey": api_key,
    }

    print("=== Request ===")
    print("URL:   ", url)
    print("PARAMS:", params)

    try:
        r = requests.get(url, params=params, timeout=20)
    except Exception as e:
        print(f"HTTP error: {e}", file=sys.stderr)
        return 1

    print("\n=== Response meta ===")
    print("Status:", r.status_code)
    print("Final URL:", r.url)

    print("\n=== Raw body (first 4000 chars) ===")
    text = r.text
    print(text[:4000])

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
