"""
Forward Shape Evaluator (NO atmosphere version)
---------------------------------------------------
For each pattern event, store what the next N minutes look like.

Outputs:
  - pattern_hits
  - pattern_forward_shape
  - pattern_feature_record (shape vector bundle, ML-ready)

Usage:
  python -m tools.eval_forward_shape_day --symbol AAPL --date 2025-08-01
"""

import os,json
from datetime import datetime,timedelta
from pathlib import Path
import numpy as np
import pyarrow.parquet as pq
import pandas as pd
from dotenv import load_dotenv
import psycopg
from psycopg.rows import dict_row

###############################################################
# ENV LOADER
###############################################################
def load_env():
    root = Path(__file__).resolve().parents[1]
    if (root/".env").exists(): load_dotenv(root/".env")

    dsn = os.getenv("REFLEX_PG_DSN")
    parquet_root = (os.getenv("REFLEX_TICK_PARQUET_ROOT")
                 or os.getenv("REFLEX_STORAGE_PARQUET_ROOT")
                 or os.getenv("PARQUET_ROOT"))

    if not dsn or not parquet_root:
        raise SystemExit("ERROR: DSN or Parquet root missing in .env")

    return dsn, Path(parquet_root)

###############################################################
# TICK LOADER
###############################################################
def load_ticks(parquet_root,symbol,date):
    f = parquet_root/symbol/f"{symbol}_{date}.parquet"
    if not f.exists():
        print(f"[WARN] no parquet: {f}")
        return None
    return pq.read_table(f).to_pandas().sort_values("timestamp")

###############################################################
# SIMPLE PATTERN DETECTOR (placeholder)
# Replace with real pattern logic later
###############################################################
def detect_patterns(df):
    events=[]
    for i,row in df.iterrows():
        # simple example: big volume = pattern
        if row.size>2000:
            events.append((row.timestamp,"big_vol",1.0,row.price))
    return events

###############################################################
# SHAPE EXTRACTION
###############################################################
def extract_forward_shape(df,event_ts,price_event,horizons=(5,10,15),stop=0.01,target=0.02):
    rows=[]
    for H in horizons:
        end=event_ts+timedelta(minutes=H)
        win=df[(df.timestamp>=event_ts)&(df.timestamp<=end)]
        if len(win)==0: continue

        prices=win.price.values
        vols=win.size.values

        ret=(prices-price_event)/price_event
        fwd=float(ret[-1])
        max_r=float(max(ret))
        max_dd=float(min(ret))

        t_ru=int(np.argmax(ret))
        t_dd=int(np.argmin(ret))

        # compress to 5 buckets (shape vector)
        ret_path=[float(np.mean(chunk)) for chunk in np.array_split(ret,5)]
        vol_path=[float(np.mean(chunk)) for chunk in np.array_split(vols,5)]

        hit_t=(ret>target).any()
        hit_s=(ret<-stop).any()

        label="rocket" if max_r>0.03 else "fade" if max_dd<-0.02 else "neutral"

        rows.append({
            "horizon":H,
            "price_event":float(price_event),
            "price_at_horizon":float(prices[-1]),
            "fwd_ret":fwd,
            "max_runup":max_r,
            "max_drawdown":max_dd,
            "time_to_max_runup":t_ru,
            "time_to_max_drawdown":t_dd,
            "vol_sum":float(vols.sum()),
            "vol_peak":float(vols.max()),
            "vol_peak_offset":int(np.argmax(vols)),
            "ret_path":ret_path,
            "vol_path":vol_path,
            "hit_target":hit_t,
            "hit_stop":hit_s,
            "shape_label":label
        })
    return rows

###############################################################
# MAIN EVAL RUNNER
###############################################################
def run_day(symbol,date,horizons=(5,10,15)):
    dsn,parq=load_env()
    ticks=load_ticks(parq,symbol,date)
    if ticks is None: return

    with psycopg.connect(dsn) as conn:
        events=detect_patterns(ticks)

        for ts,pname,strength,price in events:
            bucket=ts.replace(second=0,microsecond=0)

            shapes=extract_forward_shape(ticks,ts,price,horizons=horizons)

            # store raw hit
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO pattern_hits(symbol,event_ts,bucket_ts,pattern_name,strength,price_event)
                    VALUES(%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                """,(symbol,ts,bucket,pname,strength,price))

            # store shapes
            with conn.cursor() as cur:
                for s in shapes:
                    cur.execute("""
                        INSERT INTO pattern_forward_shape(symbol,event_ts,pattern_name,horizon_min,
                          price_event,price_at_horizon,fwd_ret,max_runup,max_drawdown,
                          time_to_max_runup,time_to_max_drawdown,vol_sum,vol_peak,vol_peak_offset,
                          ret_path,vol_path,hit_target,hit_stop,shape_label)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT DO NOTHING
                    """,(symbol,ts,pname,s["horizon"],
                         s["price_event"],s["price_at_horizon"],s["fwd_ret"],
                         s["max_runup"],s["max_drawdown"],
                         s["time_to_max_runup"],s["time_to_max_drawdown"],
                         s["vol_sum"],s["vol_peak"],s["vol_peak_offset"],
                         s["ret_path"],s["vol_path"],
                         s["hit_target"],s["hit_stop"],s["shape_label"]))

            # full research feature blob
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO pattern_feature_record(symbol,event_ts,pattern_name,price_event,horizons)
                    VALUES(%s,%s,%s,%s,%s)
                """,(symbol,ts,pname,price,json.dumps(shapes)))

            conn.commit()
            print(f"[OK] {symbol} {ts} → {len(shapes)} forward windows stored.")

    print("\n[DONE] Day complete — shape data ready for research.")

###############################################################
# CLI
###############################################################
if __name__=="__main__":
    import argparse
    ap=argparse.ArgumentParser()
    ap.add_argument("--symbol",required=True)
    ap.add_argument("--date",required=True)
    args=ap.parse_args()
    run_day(args.symbol,args.date)
