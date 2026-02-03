"""
eval_shape_range.py

Historical forward-shape evaluator (pattern-only, template-style).

- Input: tick parquet per symbol/day
- Output: pattern tables in Postgres:
    pattern_hits
    pattern_forward_shape
    pattern_feature_record

This is HISTORY ONLY. Realtime has its own bot.

Symbol selection:
    SYMBOL       -> single symbol (e.g. KROS)
    ALL          -> all symbol dirs under parquet_root
    K* / LI*     -> wildcard against symbol dirs

Date selection:
    START                 -> single date (YYYY-MM-DD)
    START END             -> inclusive range [START .. END]

Env used:
    REFLEX__PG_DSN          : Postgres DSN
    REFLEX__PARQUET_ROOT    : base parquet root (e.g. D:\reflex_parquet)
    REFLEX__INSTANCE_NAME   : instance name (e.g. live) [optional, default live]
    REFLEX__RUN_ID          : run id tag (default 'dev')
"""

from __future__ import annotations

import fnmatch
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Sequence

import numpy as np
import pandas as pd
import psycopg


# ------------------------------------------------------------
# ENV & PATH HELPERS
# ------------------------------------------------------------

RUN_ID = os.getenv("REFLEX__RUN_ID", "dev")


def get_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return val


def build_parquet_root() -> Path:
    """
    Build parquet root path from env:

        REFLEX__PARQUET_ROOT      e.g. D:\\reflex_parquet
        REFLEX__INSTANCE_NAME     e.g. live (default)

    Final path:
        {REFLEX__PARQUET_ROOT}\\instances\\{INSTANCE}
    """
    base = Path(get_env("REFLEX__PARQUET_ROOT"))
    instance = os.getenv("REFLEX__INSTANCE_NAME", "live")
    return base / "instances" / instance


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def date_range_inclusive(start: date, end: date) -> Iterable[date]:
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


# ------------------------------------------------------------
# PARQUET LOADING
# ------------------------------------------------------------

def _detect_columns(df: pd.DataFrame):
    ts_col = None
    for c in ("timestamp", "ts", "time"):
        if c in df.columns:
            ts_col = c
            break
    if ts_col is None:
        raise KeyError(f"Could not find timestamp column in {list(df.columns)}")

    px_col = None
    for c in ("price", "px"):
        if c in df.columns:
            px_col = c
            break
    if px_col is None:
        raise KeyError(f"Could not find price column in {list(df.columns)}")

    size_col = None
    for c in ("size", "sz", "volume", "qty"):
        if c in df.columns:
            size_col = c
            break
    if size_col is None:
        raise KeyError(f"Could not find size/volume column in {list(df.columns)}")

    return ts_col, px_col, size_col


def load_ticks(parquet_root: Path, symbol: str, d: date) -> pd.DataFrame | None:
    f = parquet_root / symbol / f"{symbol}_{d}.parquet"
    if not f.exists():
        print(f"[WARN] no parquet for {symbol} {d}: {f}")
        return None

    df = pd.read_parquet(f)

    ts_col, px_col, size_col = _detect_columns(df)
    df = df[[ts_col, px_col, size_col]].copy()
    df.rename(columns={ts_col: "timestamp", px_col: "price", size_col: "size"}, inplace=True)

    # ensure timezone-aware UTC
    if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
        df["timestamp"] = pd.to_datetime(df["timestamp"])
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(timezone.utc)

    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


# ------------------------------------------------------------
# SYMBOL SELECTION
# ------------------------------------------------------------

def list_all_symbols(parquet_root: Path) -> List[str]:
    if not parquet_root.exists():
        return []
    syms: List[str] = []
    for p in parquet_root.iterdir():
        if p.is_dir():
            syms.append(p.name.upper())
    return syms


def resolve_symbol_pattern(parquet_root: Path, pattern: str) -> List[str]:
    pattern = pattern.strip()
    if not pattern:
        return []

    pattern_up = pattern.upper()
    all_syms = list_all_symbols(parquet_root)

    if pattern_up in ("ALL", "*"):
        return sorted(all_syms)

    # wildcard?
    if any(ch in pattern_up for ch in "*?[]"):
        return sorted([s for s in all_syms if fnmatch.fnmatchcase(s, pattern_up)])

    # plain symbol
    if pattern_up in all_syms:
        return [pattern_up]

    # fallback: wildcard prefix
    matches = [s for s in all_syms if fnmatch.fnmatchcase(s, pattern_up + "*")]
    return sorted(matches)


# ------------------------------------------------------------
# PATTERN DETECTION
# ------------------------------------------------------------

@dataclass
class PatternEvent:
    symbol: str
    index: int
    event_ts: datetime
    bucket_ts: datetime
    price_event: float
    strength: float
    pattern_name: str = "big_vol"


def find_big_vol_events(df: pd.DataFrame, symbol: str) -> List[PatternEvent]:
    """
    Simple template pattern: top 1% notional volume prints.
    This is deliberately crude – meant as a pluggable pattern hook.
    """
    if df.empty:
        return []

    notional = df["price"] * df["size"]
    threshold = np.nanpercentile(notional, 99)  # top 1%

    hits = np.where(notional >= threshold)[0]
    events: List[PatternEvent] = []

    for idx in hits:
        row = df.iloc[idx]
        ts: datetime = row["timestamp"].to_pydatetime()
        price = float(row["price"])
        strength = float(notional.iloc[idx])
        bucket_ts = ts.replace(second=0, microsecond=0)

        events.append(
            PatternEvent(
                symbol=symbol,
                index=int(idx),
                event_ts=ts,
                bucket_ts=bucket_ts,
                price_event=price,
                strength=strength,
                pattern_name="big_vol",
            )
        )

    return events


# ------------------------------------------------------------
# FORWARD SHAPE
# ------------------------------------------------------------

@dataclass
class ForwardShape:
    horizon_min: int
    price_event: float
    price_at_horizon: float
    fwd_ret: float
    max_runup: float
    max_drawdown: float
    time_to_max_runup: int
    time_to_max_drawdown: int
    vol_sum: float
    vol_peak: float
    vol_peak_offset: int
    ret_path: List[float]
    vol_path: List[float]
    hit_target: bool
    hit_stop: bool
    shape_label: str


def compute_forward_shapes(
    df: pd.DataFrame,
    event_idx: int,
    horizons: Sequence[int],
    target_ret: float = 0.05,
    stop_ret: float = -0.03,
) -> List[ForwardShape]:
    if df.empty:
        return []

    ts0 = df.loc[event_idx, "timestamp"]
    p0 = float(df.loc[event_idx, "price"])

    window_max_h = max(horizons)
    horizon_end = ts0 + timedelta(minutes=window_max_h)
    window = df[(df["timestamp"] > ts0) & (df["timestamp"] <= horizon_end)].copy()

    if window.empty:
        return []

    window["ret"] = (window["price"] - p0) / p0
    window["notional"] = window["price"] * window["size"]

    out: List[ForwardShape] = []

    for h in horizons:
        h_end = ts0 + timedelta(minutes=h)
        w = window[window["timestamp"] <= h_end]
        if w.empty:
            # keep structure, mark "no_data"
            ret_path = [float("nan")] * 5
            vol_path = [float("nan")] * 5
            out.append(
                ForwardShape(
                    horizon_min=h,
                    price_event=p0,
                    price_at_horizon=p0,
                    fwd_ret=0.0,
                    max_runup=0.0,
                    max_drawdown=0.0,
                    time_to_max_runup=0,
                    time_to_max_drawdown=0,
                    vol_sum=0.0,
                    vol_peak=0.0,
                    vol_peak_offset=0,
                    ret_path=ret_path,
                    vol_path=vol_path,
                    hit_target=False,
                    hit_stop=False,
                    shape_label="no_data",
                )
            )
            continue

        rets = w["ret"].to_numpy()
        vols = w["notional"].to_numpy()

        p_h = float(w.iloc[-1]["price"])
        fwd_ret = float(rets[-1])

        cum_max = np.maximum.accumulate(rets)
        cum_min = np.minimum.accumulate(rets)

        max_runup = float(cum_max.max()) if len(cum_max) else 0.0
        max_drawdown = float(cum_min.min()) if len(cum_min) else 0.0
        time_to_max_runup = int(np.argmax(cum_max)) if len(cum_max) else 0
        time_to_max_drawdown = int(np.argmin(cum_min)) if len(cum_min) else 0

        vol_sum = float(vols.sum()) if len(vols) else 0.0
        if len(vols):
            vol_peak_idx = int(np.argmax(vols))
            vol_peak = float(vols[vol_peak_idx])
        else:
            vol_peak_idx = 0
            vol_peak = 0.0

        vol_peak_offset = vol_peak_idx

        # coarse 5-bucket paths
        ret_chunks = np.array_split(rets, 5)
        vol_chunks = np.array_split(vols, 5)
        ret_path = [float(c.mean()) if len(c) else float("nan") for c in ret_chunks]
        vol_path = [float(c.mean()) if len(c) else float("nan") for c in vol_chunks]

        hit_target = bool((rets >= target_ret).any())
        hit_stop = bool((rets <= stop_ret).any())

        if hit_target and not hit_stop:
            label = "rocket_up"
        elif hit_stop and not hit_target:
            label = "flush_down"
        elif hit_target and hit_stop:
            label = "whipsaw"
        else:
            label = "meh"

        out.append(
            ForwardShape(
                horizon_min=h,
                price_event=p0,
                price_at_horizon=p_h,
                fwd_ret=fwd_ret,
                max_runup=max_runup,
                max_drawdown=max_drawdown,
                time_to_max_runup=time_to_max_runup,
                time_to_max_drawdown=time_to_max_drawdown,
                vol_sum=vol_sum,
                vol_peak=vol_peak,
                vol_peak_offset=vol_peak_offset,
                ret_path=ret_path,
                vol_path=vol_path,
                hit_target=hit_target,
                hit_stop=hit_stop,
                shape_label=label,
            )
        )

    return out


# ------------------------------------------------------------
# DB WRITE HELPERS
# ------------------------------------------------------------

def _clean_for_json(obj):
    """
    Recursively walk lists/dicts and replace NaN/inf floats
    with None so JSON/JSONB will accept them.
    """
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj

    if isinstance(obj, list):
        return [_clean_for_json(v) for v in obj]

    if isinstance(obj, dict):
        return {k: _clean_for_json(v) for k, v in obj.items()}

    return obj


def upsert_pattern_hit(conn: psycopg.Connection, ev: PatternEvent) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pattern_hits(run_id,symbol,event_ts,bucket_ts,pattern_name,strength,price_event)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (run_id,symbol,event_ts,pattern_name)
            DO UPDATE SET
                bucket_ts = EXCLUDED.bucket_ts,
                strength = EXCLUDED.strength,
                price_event = EXCLUDED.price_event
            """,
            (
                RUN_ID,
                ev.symbol,
                ev.event_ts,
                ev.bucket_ts,
                ev.pattern_name,
                ev.strength,
                ev.price_event,
            ),
        )


def upsert_forward_shape(
    conn: psycopg.Connection,
    symbol: str,
    event_ts: datetime,
    pattern_name: str,
    shape: ForwardShape,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pattern_forward_shape(
                run_id,symbol,event_ts,pattern_name,horizon_min,
                price_event,price_at_horizon,fwd_ret,
                max_runup,max_drawdown,
                time_to_max_runup,time_to_max_drawdown,
                vol_sum,vol_peak,vol_peak_offset,
                ret_path,vol_path,
                hit_target,hit_stop,shape_label
            )
            VALUES (
                %s,%s,%s,%s,%s,
                %s,%s,%s,
                %s,%s,
                %s,%s,
                %s,%s,%s,
                %s,%s,
                %s,%s,%s
            )
            ON CONFLICT (run_id,symbol,event_ts,pattern_name,horizon_min)
            DO UPDATE SET
                price_event = EXCLUDED.price_event,
                price_at_horizon = EXCLUDED.price_at_horizon,
                fwd_ret = EXCLUDED.fwd_ret,
                max_runup = EXCLUDED.max_runup,
                max_drawdown = EXCLUDED.max_drawdown,
                time_to_max_runup = EXCLUDED.time_to_max_runup,
                time_to_max_drawdown = EXCLUDED.time_to_max_drawdown,
                vol_sum = EXCLUDED.vol_sum,
                vol_peak = EXCLUDED.vol_peak,
                vol_peak_offset = EXCLUDED.vol_peak_offset,
                ret_path = EXCLUDED.ret_path,
                vol_path = EXCLUDED.vol_path,
                hit_target = EXCLUDED.hit_target,
                hit_stop = EXCLUDED.hit_stop,
                shape_label = EXCLUDED.shape_label
            """,
            (
                RUN_ID,
                symbol,
                event_ts,
                pattern_name,
                shape.horizon_min,
                shape.price_event,
                shape.price_at_horizon,
                shape.fwd_ret,
                shape.max_runup,
                shape.max_drawdown,
                shape.time_to_max_runup,
                shape.time_to_max_drawdown,
                shape.vol_sum,
                shape.vol_peak,
                shape.vol_peak_offset,
                shape.ret_path,
                shape.vol_path,
                shape.hit_target,
                shape.hit_stop,
                shape.shape_label,
            ),
        )


def insert_feature_record(
    conn: psycopg.Connection,
    symbol: str,
    event_ts: datetime,
    pattern_name: str,
    price_event: float,
    shapes: List[ForwardShape],
) -> None:
    payload = [
        {
            "horizon_min": s.horizon_min,
            "price_event": s.price_event,
            "price_at_horizon": s.price_at_horizon,
            "fwd_ret": s.fwd_ret,
            "max_runup": s.max_runup,
            "max_drawdown": s.max_drawdown,
            "time_to_max_runup": s.time_to_max_runup,
            "time_to_max_drawdown": s.time_to_max_drawdown,
            "vol_sum": s.vol_sum,
            "vol_peak": s.vol_peak,
            "vol_peak_offset": s.vol_peak_offset,
            "ret_path": s.ret_path,
            "vol_path": s.vol_path,
            "hit_target": s.hit_target,
            "hit_stop": s.hit_stop,
            "shape_label": s.shape_label,
        }
        for s in shapes
    ]
    payload = _clean_for_json(payload)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pattern_feature_record(run_id,symbol,event_ts,pattern_name,price_event,horizons)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (run_id,symbol,event_ts,pattern_name)
            DO UPDATE SET
                price_event = EXCLUDED.price_event,
                horizons = EXCLUDED.horizons
            """,
            (RUN_ID, symbol, event_ts, pattern_name, price_event, json.dumps(payload)),
        )


# ------------------------------------------------------------
# MAIN EXECUTION
# ------------------------------------------------------------

def run_one_day_for_symbol(
    conn: psycopg.Connection,
    parquet_root: Path,
    symbol: str,
    d: date,
    horizons: Sequence[int],
) -> None:
    print(f"[RUN] {symbol} {d}")
    df = load_ticks(parquet_root, symbol, d)
    if df is None or df.empty:
        return

    events = find_big_vol_events(df, symbol)
    if not events:
        return

    for ev in events:
        shapes = compute_forward_shapes(df, ev.index, horizons=horizons)
        if not shapes:
            continue

        upsert_pattern_hit(conn, ev)
        for s in shapes:
            upsert_forward_shape(conn, ev.symbol, ev.event_ts, ev.pattern_name, s)
        insert_feature_record(conn, ev.symbol, ev.event_ts, ev.pattern_name, ev.price_event, shapes)

        print(
            f"[OK] {symbol} {d} {ev.event_ts} {ev.pattern_name} → {len(shapes)} forward windows stored"
        )

    conn.commit()


def run_for_symbols_and_range(
    symbols: Sequence[str],
    start: date,
    end: date,
    horizons: Sequence[int],
) -> None:
    parquet_root = build_parquet_root()
    dsn = get_env("REFLEX__PG_DSN")

    with psycopg.connect(dsn) as conn:
        for sym in symbols:
            sym_up = sym.upper()
            for d in date_range_inclusive(start, end):
                run_one_day_for_symbol(conn, parquet_root, sym_up, d, horizons=horizons)


# ------------------------------------------------------------
# CLI
# ------------------------------------------------------------

def usage() -> None:
    print(
        "Usage:\n"
        "  python -m evaluator.bots.abots.eval_shape_range SYMBOL_OR_PATTERN START [END]\n"
        "\n"
        "Examples:\n"
        "  # one symbol, one day\n"
        "  eval_shape_range KROS 2025-08-01\n"
        "\n"
        "  # one symbol, range inclusive\n"
        "  eval_shape_range KROS 2025-08-01 2025-12-05\n"
        "\n"
        "  # all symbols, one day\n"
        "  eval_shape_range ALL 2025-11-25\n"
        "\n"
        "  # wildcard pattern, range\n"
        "  eval_shape_range K* 2025-08-01 2025-08-31\n"
    )


def main(argv: Sequence[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    if len(argv) < 2 or len(argv) > 3:
        usage()
        sys.exit(1)

    symbol_pattern = argv[0]
    start_str = argv[1]
    end_str = argv[2] if len(argv) == 3 else None

    start = parse_date(start_str)
    end = parse_date(end_str) if end_str else start

    horizons = (5, 10, 15)  # template: adjust later as needed

    parquet_root = build_parquet_root()
    symbols = resolve_symbol_pattern(parquet_root, symbol_pattern)

    if not symbols:
        print(f"[WARN] No symbols matched pattern '{symbol_pattern}' under {parquet_root}")
        return

    print(f"[INFO] RUN_ID={RUN_ID}")
    print(f"[INFO] Symbols: {', '.join(symbols)}")
    print(f"[INFO] Dates: {start} .. {end} (inclusive)")

    run_for_symbols_and_range(symbols, start, end, horizons=horizons)


if __name__ == "__main__":
    main()
