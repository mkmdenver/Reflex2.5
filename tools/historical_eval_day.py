"""
Historical One-Day Evaluator
───────────────────────────────────────────────
Purpose:
  Walk one symbol for one day from Parquet ticks, enrich with DB context,
  inspect patterns, and output any triggers with stats.

Usage:
  python -m tools.historical_eval_day --symbol AAPL --date 2025-08-05
"""

import os
import csv
import argparse
import datetime as dt
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv
import pyarrow.parquet as pq
import pandas as pd


#-----------------------------------------------#
# env
#-----------------------------------------------#
def env_load():
    root = Path(__file__).resolve().parents[1]
    env = root / ".env"
    if env.exists():
        load_dotenv(env)

    dsn = os.getenv("REFLEX_PG_DSN")
    if not dsn:
        raise SystemExit("REFLEX_PG_DSN missing in .env")

    parquet_root = (
        os.getenv("REFLEX_TICK_PARQUET_ROOT")
        or os.getenv("REFLEX_STORAGE_PARQUET_ROOT")
        or os.getenv("PARQUET_ROOT")
    )
    if not parquet_root:
        raise SystemExit("No parquet root configured in .env")

    return dsn, Path(parquet_root)


#-----------------------------------------------#
# DB fetch helpers
#-----------------------------------------------#
def db_fetch_daily(conn, symbol, date):
    q = """
        SELECT *
        FROM daily_bars
        WHERE symbol=%s AND date(timestamp)=%s
        LIMIT 1
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(q, (symbol, date))
        return cur.fetchone()


def db_fetch_minute_bars(conn, symbol, date):
    q = """
        SELECT *
        FROM minute_bars
        WHERE symbol=%s AND date(timestamp)=%s
        ORDER BY timestamp
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(q, (symbol, date))
        return cur.fetchall()


def db_fetch_fundamentals(conn, symbol):
    q = "SELECT * FROM fundamental_data WHERE symbol=%s"
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(q, (symbol,))
        return cur.fetchone()


def db_fetch_prev_close(conn, symbol, date):
    q = """
        SELECT prev_close_adj
        FROM mv_prev_close_adj
        WHERE symbol=%s AND session_date=%s
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(q, (symbol, date))
        row = cur.fetchone()
        return row["prev_close_adj"] if row else None


#-----------------------------------------------#
# tick loader (from Parquet)
#-----------------------------------------------#
def load_ticks(parquet_root, symbol, date):
    p = parquet_root / symbol / f"{symbol}_{date}.parquet"
    if not p.exists():
        print(f"[ERR] Tick file missing: {p}")
        return None
    return pq.read_table(p).to_pandas()  # ~250k rows/sec local


#-----------------------------------------------#
# Example pattern detector (placeholder)
#-----------------------------------------------#
def detect_pattern(row, prev_close):
    """
    Replace with real strategy logic.
    Something simple for now:
        Trigger if >+3% from prev close & volume spike in that minute.
    """
    price = float(row.price)
    pct = ((price / prev_close) - 1) * 100 if prev_close else 0

    if pct > 3.0:  # <-- replace with real logic soon
        return {
            "pattern": "gap_run",
            "pct_up": round(pct,2),
        }
    return None


#-----------------------------------------------#
# evaluation loop
#-----------------------------------------------#
def run_day(symbol, date):
    dsn, parquet_root = env_load()
    print(f"\n[RUN] Evaluating {symbol} @ {date}")

    with psycopg.connect(dsn) as conn:
        daily  = db_fetch_daily(conn, symbol, date)
        mins   = db_fetch_minute_bars(conn, symbol, date)
        fund   = db_fetch_fundamentals(conn, symbol)
        pclose = db_fetch_prev_close(conn, symbol, date)

        print(f"[INFO] Prev close={pclose}")
        print(f"[INFO] Minute bars={len(mins)} rows")
        print(f"[INFO] Fundamentals loaded")

        ticks = load_ticks(parquet_root, symbol, date)
        if ticks is None or len(ticks)==0:
            print("[WARN] no ticks found")
            return

        # MUST be sorted (pyarrow returns ordered usually)
        ticks = ticks.sort_values("timestamp")

        # output
        out_dir = Path("results/history_triggers")
        out_dir.mkdir(parents=True, exist_ok=True)
        outfile = out_dir / f"{symbol}_{date}_triggers.csv"

        with open(outfile,"w",newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "timestamp","symbol","price","size",
                "pattern","pct_up",
                "float","sector","market_cap"
            ])

            for _, row in ticks.iterrows():
                event = detect_pattern(row, pclose)
                if event:
                    w.writerow([
                        row.timestamp, symbol, row.price,row.size,
                        event["pattern"],event.get("pct_up",""),
                        fund.get("shares_float") if fund else "",
                        fund.get("sector") if fund else "",
                        fund.get("market_cap") if fund else "",
                    ])

        print(f"[DONE] triggers → {outfile}")


#-----------------------------------------------#
# cli
#-----------------------------------------------#
if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--symbol",required=True)
    ap.add_argument("--date",required=True)   # YYYY-MM-DD
    a=ap.parse_args()

    run_day(a.symbol,dt.datetime.strptime(a.date,"%Y-%m-%d").date())
