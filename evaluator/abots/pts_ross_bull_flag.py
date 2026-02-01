"""pts_ross_bull_flag.py

Historical forward-shape evaluator for a Ross-Cameron-style bull flag.

- Input: tick parquet per symbol/day
- Output: pattern tables in Postgres:
    pattern_hits
    pattern_forward_shape
    pattern_feature_record

This is HISTORY ONLY. Realtime will have its own bot.

Symbol selection:
    SYMBOL       -> single symbol (e.g. KROS)
    ALL          -> all symbol dirs under parquet root
    K* / LI*     -> wildcard against symbol dirs

Date selection:
    START                 -> single date (YYYY-MM-DD)
    START END             -> inclusive range [START .. END]

Env used (no double-underscore names; aligned with project .env):
    REFLEX_PG_DSN                : Postgres DSN (e.g. postgresql://...)
    REFLEX_STORAGE_PARQUET_ROOT  : preferred parquet root
    PARQUET_ROOT                 : fallback parquet root
    REFLEX_INSTANCE_ID           : instance name (optional, for logging only)
    REFLEX_RUN_ID                : run id tag (optional, default 'ross_bull_flag_dev')

Notes (2026-01):
- Added "core shape" trade metrics (TTS / early MAE / time underwater / TFP / CLUT / RPM, etc.).
- No time buckets here: we emit raw per-event metrics into pattern_feature_record JSON.
- 10-minute horizon is treated as a hard timeout (forced exit) for trade-like stats.
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
from typing import Iterable, List, Sequence, Tuple

import numpy as np
import pandas as pd
import psycopg


# ------------------------------------------------------------
# ENV & PATH HELPERS
# ------------------------------------------------------------

def get_run_id() -> str:
    """
    Run id for pattern rows.

    Behavior:
      - If REFLEX_RUN_ID is set, treat it as the *base*.
      - Append a date+hour-minute suffix for natural rerun sequencing: _YYYYMMDD_HHMM
      - Set REFLEX_RUN_ID_NO_SUFFIX=1 to keep the base id unchanged (old behavior).
      - Set REFLEX_RUN_ID_SUFFIX_TZ=UTC to suffix in UTC (default is local time).
    """
    base = (
        os.getenv("REFLEX_RUN_ID")
        or os.getenv("REFLEX_RUN_ID".replace("__", "_"))  # just in case
        or "ross_bull_flag_dev"
    )

    if os.getenv("REFLEX_RUN_ID_NO_SUFFIX", "0") == "1":
        return base

    tz = os.getenv("REFLEX_RUN_ID_SUFFIX_TZ", "").strip().upper()
    if tz == "UTC":
        now = datetime.now(timezone.utc)
    else:
        # local time (matches your “today / this run” mental model)
        now = datetime.now()

    suffix = now.strftime("%Y%m%d_%H%M")

    # Avoid double-suffix if the user already put a suffix on the base.
    # (Very lightweight check: endswith _NNNNNNNN_NNNN)
    if len(base) >= 14 and base[-13] == "_" and base[-8] == "_":
        tail = base[-13:]
        if tail.replace("_", "").isdigit():
            return base

    return f"{base}_{suffix}"



RUN_ID = get_run_id()

# Debug logging gate (set REFLEX_DEBUG=1 for verbose per-bar output)
DEBUG = bool(int(os.getenv("REFLEX_DEBUG", "0")))



def require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return val


def resolve_pg_dsn() -> str:
    """
    Prefer REFLEX_PG_DSN, with a couple of fallbacks.

    This matches the project's .env which defines:
        REFLEX_PG_DSN=postgresql://...
    """
    for key in ("REFLEX_PG_DSN", "PG_DSN"):
        val = os.getenv(key)
        if val:
            return val

    # As a very last resort, allow bare libpq-style envs, but only if all are present
    host = os.getenv("PGHOST")
    user = os.getenv("PGUSER")
    password = os.getenv("PGPASSWORD")
    db = os.getenv("PGDATABASE")
    port = os.getenv("PGPORT", "5432")
    if host and user and password and db:
        return f"postgresql://{user}:{password}@{host}:{port}/{db}"

    raise RuntimeError("No Postgres DSN found. Set REFLEX_PG_DSN or PG_DSN.")


def resolve_parquet_root() -> Path:
    """
    Prefer REFLEX_STORAGE_PARQUET_ROOT, then PARQUET_ROOT.

    In your .env today we have:
        REFLEX_STORAGE_PARQUET_ROOT=D:\\reflex_parquet\\instances\\liveA
        PARQUET_ROOT=D:\\market
    """
    for key in ("REFLEX_STORAGE_PARQUET_ROOT", "PARQUET_ROOT"):
        val = os.getenv(key)
        if val:
            return Path(val)

    raise RuntimeError(
        "No parquet root configured. Set REFLEX_STORAGE_PARQUET_ROOT or PARQUET_ROOT."
    )


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

def _detect_columns(df: pd.DataFrame) -> Tuple[str, str, str]:
    ts_col = None
    for c in ("timestamp", "ts", "time"):
        if c in df.columns:
            ts_col = c
            break
    if ts_col is None:
        raise KeyError(f"Could not find timestamp column in {list(df.columns)}")

    px_col = None
    for c in ("price", "px", "last"):
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
    print(f"[INFO] loading ticks for {symbol} {d}")
    """
    Expects layout:
        {parquet_root}/{SYMBOL}/{SYMBOL}_{YYYY-MM-DD}.parquet
    """
    f = parquet_root / symbol / f"{symbol}_{d}.parquet"
    if not f.exists():
        print(f"[WARN] no parquet for {symbol} {d}: {f}")
        return None

    df = pd.read_parquet(f)

    ts_col, px_col, size_col = _detect_columns(df)
    df = df[[ts_col, px_col, size_col]].copy()
    df.rename(
        columns={ts_col: "timestamp", px_col: "price", size_col: "size"}, inplace=True
    )

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
    print(f"[INFO] resolving symbol pattern: {pattern}")
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
# PATTERN: ROSS-STYLE BULL FLAG (APPROX)
# ------------------------------------------------------------

@dataclass
class PatternEvent:
    symbol: str
    index: int  # index into original tick df
    event_ts: datetime  # breakout tick ts
    bucket_ts: datetime
    price_event: float
    strength: float  # approximate pole % move

    # Tagging for research: primary vs add, and parent linkage for adds.
    entry_kind: str = "primary"              # "primary" | "add"
    parent_event_ts: datetime | None = None  # for adds: the primary event_ts

    pattern_name: str = "ross_bull_flag"


def find_ross_bull_flag_events(df: pd.DataFrame, symbol: str) -> List[PatternEvent]:
    """
    Approximate Ross-Cameron-style bull flag using 1-second bars:

    1) Strong 2-minute upswing from a local low:
       - price_now / low_2m - 1 >= pole_ret_min

    2) Shallow pullback + consolidation in the last ~45s:
       - prior 45s range shows a pullback of 1-40% off local high

    3) Breakout:
       - current price > prior 45s high * (1 + breakout_eps)
       - current notional volume >= a day-level percentile threshold

    NOTE: This detector is intentionally approximate; the real value is in
    forward-shape + trade-like metrics for research.
    """
    events: List[PatternEvent] = []

    if df.empty:
        return events

    ticks = df.copy()
    ticks["notional"] = ticks["price"] * ticks["size"]
    s = ticks.set_index("timestamp")

    # 1-second bars: last price, sum size/notional
    # IMPORTANT: densify to a true 1-second timeline.
    # Our tick data only has rows for seconds that traded. If we drop empty
    # seconds, time-based rolling windows (e.g. "120s") become liquidity-dependent
    # and can produce NaNs for most of the day on thin names.
    bars = s.resample("1s").agg({"price": "last", "size": "sum", "notional": "sum"})

    # Forward-fill last trade price across empty seconds; treat size/notional as 0.
    bars["price"] = bars["price"].ffill()
    bars[["size", "notional"]] = bars[["size", "notional"]].fillna(0.0)

    # Drop any leading pre-first-trade seconds that still have no price.
    bars = bars.dropna(subset=["price"])
    if bars.empty:
        return events

    # core parameters (tunable)
    pole_window = "120s"  # lookback for the pole (2 minutes)
    flag_window = 45  # seconds of consolidation we're looking back
    min_pole_ret = 0.04  # >= 6% move in 2 minutes
    max_flag_pullback = 0.40  # <= 40% retrace from local high
    breakout_eps = 0.001  # breakout slightly above prior high
    vol_percentile = 70  # breakout bar notional >= this day percentile
    min_pole_trades = 10  # minimum traded-seconds in the 2-minute window to trust swing_low

    # Identify seconds that actually traded (used for gating + iteration).
    # Note: after we densify and fill, size/notional are 0 for empty seconds.
    bars["traded"] = bars["notional"] > 0

    # 2-minute swing low for pole (computed on dense 1s price, so it's always defined once price exists).
    # We *gate* pole detection using trade_count_2m so thin names don't fabricate signals off pure forward-fill.
    bars["trade_count_2m"] = bars["traded"].rolling(pole_window, min_periods=1).sum()
    bars["swing_low_2m"] = bars["price"].rolling(pole_window, min_periods=1).min()
    bars["ret_from_2m_low"] = bars["price"] / bars["swing_low_2m"] - 1.0



    # day-level notional threshold for "decent" volume
    # Use non-zero notional when possible; otherwise a day full of zero-volume
    # seconds would make the percentile trivially 0 and defeat the filter.
    notional_arr = bars["notional"].to_numpy(dtype=float)
    nz = notional_arr[notional_arr > 0]
    if nz.size >= 10:
        vol_threshold = float(np.nanpercentile(nz, vol_percentile))
    else:
        vol_threshold = float(np.nanpercentile(notional_arr, vol_percentile)) if notional_arr.size else 0.0


    print(
        f"[INFO] evaluating {len(bars)} bars for symbol {symbol}: "
        f"vol_threshold={vol_threshold}, pole_window={pole_window}, flag_window={flag_window}, "
        f"min_pole_ret={min_pole_ret}, max_flag_pullback={max_flag_pullback}, "
        f"breakout_eps={breakout_eps}, vol_percentile={vol_percentile}"
    )

    # Evaluate only traded seconds for pattern logic (dense bars exist only to make rolling windows stable).
    bars_eval = bars[bars["traded"]]

    # --- de-dupe + add state ---
    primary_min_gap_sec = int(os.getenv("RBF_PRIMARY_MIN_GAP_SEC", "60"))
    add_enabled = os.getenv("RBF_ENABLE_ADDS", "1") != "0"
    add_cooldown_sec = int(os.getenv("RBF_ADD_COOLDOWN_SEC", "20"))
    add_flag_window_sec = int(os.getenv("RBF_ADD_FLAG_WINDOW_SEC", "25"))
    add_max_pullback = float(os.getenv("RBF_ADD_MAX_PULLBACK", "0.15"))
    add_min_pullback = float(os.getenv("RBF_ADD_MIN_PULLBACK", "0.005"))
    add_breakout_eps = float(os.getenv("RBF_ADD_BREAKOUT_EPS", str(breakout_eps)))
    add_min_gap_sec = int(os.getenv("RBF_ADD_MIN_GAP_SEC", "30"))
    add_search_max_sec = int(os.getenv("RBF_ADD_SEARCH_MAX_SEC", "600"))  # stop looking for adds after N seconds

    last_primary_event_ts: datetime | None = None   # tick-time of primary event
    last_primary_trigger_ts: datetime | None = None # bar-time (second) trigger ts
    last_primary_price_event: float | None = None
    next_add_earliest_ts: datetime | None = None
    last_add_event_ts: datetime | None = None

    for ts, row in bars_eval.iterrows():
        if DEBUG:
            print(
                f"[DBG] {symbol} {ts} px={row['price']} "
                f"low2m={row['swing_low_2m']} ret2m={row['ret_from_2m_low']} "
                f"notional={row['notional']} trades2m={row.get('trade_count_2m', 0)}"
            )

        # -------------------------
        # PRIMARY RBF DETECTION
        # -------------------------
        swing_low = row["swing_low_2m"]
        if not np.isfinite(swing_low) or swing_low <= 0:
            continue

        if float(row.get("trade_count_2m", 0)) < min_pole_trades:
            continue

        price_now = float(row["price"])
        swing_ret = float(row["ret_from_2m_low"])
        if swing_ret < min_pole_ret:
            continue

        # prior ~45s for the flag consolidation shape
        window_start = ts - timedelta(seconds=flag_window)
        window_end = ts - timedelta(seconds=5)  # small gap before breakout bar
        w_flag = bars[(bars.index >= window_start) & (bars.index <= window_end)]
        if len(w_flag) < 10:
            continue

        flag_high = float(w_flag["price"].max())
        flag_low = float(w_flag["price"].min())
        if flag_high <= 0:
            continue

        retr_from_high = (flag_high - flag_low) / flag_high

        # require "flag-like" behavior: some pullback, but not a full reset
        if retr_from_high < 0.01:
            continue
        if retr_from_high > max_flag_pullback:
            continue

        # breakout: price clears the flag high with a small cushion
        if price_now <= flag_high * (1.0 + breakout_eps):
            continue

        # volume confirmation on the breakout bar
        if float(row["notional"]) < vol_threshold:
            continue

        # primary de-dupe (bar-time trigger spacing)
        if last_primary_trigger_ts is not None:
            if (ts - last_primary_trigger_ts).total_seconds() < primary_min_gap_sec:
                continue

        # Map breakout time back to tick index: first tick at or after this bar
        mask_ticks = ticks["timestamp"] >= ts
        if not mask_ticks.any():
            continue

        event_idx = int(mask_ticks.idxmax())
        tick_row = ticks.loc[event_idx]

        event_ts = tick_row["timestamp"].to_pydatetime()
        bucket_ts = event_ts.replace(second=0, microsecond=0)
        price_event = float(tick_row["price"])

        events.append(
            PatternEvent(
                symbol=symbol,
                index=event_idx,
                event_ts=event_ts,
                bucket_ts=bucket_ts,
                price_event=price_event,
                strength=float(swing_ret),
                entry_kind="primary",
                parent_event_ts=None,
                pattern_name="ross_bull_flag",
            )
        )

        # prime add state off the primary
        last_primary_event_ts = event_ts
        last_primary_trigger_ts = ts
        last_primary_price_event = price_event
        next_add_earliest_ts = ts + timedelta(seconds=add_cooldown_sec)
        last_add_event_ts = None

        # continue scanning (you may still find later primaries after min_gap)

        # -------------------------
        # ADD DETECTION (micro-flag)
        # -------------------------
        # Adds are searched in subsequent iterations (not in the same bar).
        # We intentionally keep adds "close" to the primary: short window + tight pullback + above primary price.

    # Second pass for adds: scan forward after each primary trigger.
    # This keeps primary detection clean and makes add logic easier to reason about.
    if add_enabled and events:
        primaries = [e for e in events if e.entry_kind == "primary"]
        for p in primaries:
            # bar-time trigger approximation: align to the first traded-second >= primary tick time
            p_trigger_candidates = bars_eval.index[bars_eval.index >= p.event_ts]
            if len(p_trigger_candidates) == 0:
                continue
            p_trigger = p_trigger_candidates[0]

            add_start = p_trigger + timedelta(seconds=add_cooldown_sec)
            add_end = p_trigger + timedelta(seconds=add_search_max_sec)

            last_add_ts_local: datetime | None = None

            add_scan = bars_eval[(bars_eval.index >= add_start) & (bars_eval.index <= add_end)]
            for ts, row in add_scan.iterrows():
                if last_add_ts_local is not None and (ts - last_add_ts_local).total_seconds() < add_min_gap_sec:
                    continue

                price_now = float(row["price"])

                # must be meaningfully above the primary entry price to qualify as an add attempt
                if price_now <= float(p.price_event) * 1.001:
                    continue

                # micro flag: shorter consolidation window
                window_start = ts - timedelta(seconds=add_flag_window_sec)
                window_end = ts - timedelta(seconds=3)
                w = bars[(bars.index >= window_start) & (bars.index <= window_end)]
                if len(w) < 10:
                    continue

                hi = float(w["price"].max())
                lo = float(w["price"].min())
                if hi <= 0:
                    continue

                pullback = (hi - lo) / hi
                if pullback < add_min_pullback:
                    continue
                if pullback > add_max_pullback:
                    continue

                # breakout above micro-high
                if price_now <= hi * (1.0 + add_breakout_eps):
                    continue

                # softer volume confirmation for adds
                if float(row["notional"]) < (vol_threshold * 0.50):
                    continue

                mask_ticks = ticks["timestamp"] >= ts
                if not mask_ticks.any():
                    continue
                event_idx = int(mask_ticks.idxmax())
                tick_row = ticks.loc[event_idx]

                event_ts = tick_row["timestamp"].to_pydatetime()

                # global add de-dupe
                if last_add_event_ts is not None and (event_ts - last_add_event_ts).total_seconds() < add_min_gap_sec:
                    continue

                bucket_ts = event_ts.replace(second=0, microsecond=0)
                price_event = float(tick_row["price"])
                strength = (price_event / lo - 1.0) if lo > 0 else 0.0

                events.append(
                    PatternEvent(
                        symbol=symbol,
                        index=event_idx,
                        event_ts=event_ts,
                        bucket_ts=bucket_ts,
                        price_event=price_event,
                        strength=float(strength),
                        entry_kind="add",
                        parent_event_ts=p.event_ts,
                        pattern_name="ross_bull_flag_add",
                    )
                )

                last_add_ts_local = ts
                last_add_event_ts = event_ts

    return events
    return events


# ------------------------------------------------------------
# FORWARD SHAPE WITH CORE SHAPE METRICS
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
    ret_path: list[float]
    vol_path: list[float]
    hit_target: bool
    hit_stop: bool
    shape_label: str

    # advanced shape / risk metrics (JSON only; not separate DB columns)
    dd_before_runup: float
    ru_over_dd: float
    frac_above_entry: float
    frac_below_entry: float

    # --- Trade lifecycle / shape metrics (JSON only) ---
    # All times are seconds from entry tick.
    safety_ret: float
    tts_sec: int | None                 # Time to Safety
    early_mae_to_safety: float | None   # Worst ret before safety (or before exit if never safe)
    time_underwater_to_safety: int | None
    tfp_sec: int | None                 # Time to First Profit (first ret > 0)

    clut_sec: int                       # Capital Lock-Up Time (exit - entry)
    exit_reason: str                    # 'target' | 'stop' | 'timeout'
    realized_r: float                   # realized return at exit
    rpm: float | None                   # realized_r / (clut_sec/60)

    efficiency: float | None            # realized_r / MFE
    mae_over_mfe: float | None          # abs(MAE) / MFE

    direction_changes: int | None
    slope_per_min: float | None
    slope_r2: float | None

    vol_expand_sec: int | None
    vol_expand_before_safety: bool | None
    vol_peak_sec: int | None


def compute_forward_shapes(
    df: pd.DataFrame,
    event_idx: int,
    horizons: Sequence[int],
    target_ret: float = 0.05,
    stop_ret: float = -0.03,
    safe_ret: float | None = None,
) -> List[ForwardShape]:
    """
    Compute forward-shape windows plus trade-like stats.

    - Entry is the event tick (event_idx)
    - For trade-like metrics we treat the horizon window as a hard timeout.

    safe_ret defaults to target_ret (common first pass: safety == +0.05R).
    """
    if df.empty:
        return []

    ts0 = df.loc[event_idx, "timestamp"]
    p0 = float(df.loc[event_idx, "price"])

    safe_ret_val = float(target_ret if safe_ret is None else safe_ret)

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
                    dd_before_runup=0.0,
                    ru_over_dd=0.0,
                    frac_above_entry=0.0,
                    frac_below_entry=0.0,
                    safety_ret=safe_ret_val,
                    tts_sec=None,
                    early_mae_to_safety=None,
                    time_underwater_to_safety=None,
                    tfp_sec=None,
                    clut_sec=0,
                    exit_reason="no_data",
                    realized_r=0.0,
                    rpm=None,
                    efficiency=None,
                    mae_over_mfe=None,
                    direction_changes=None,
                    slope_per_min=None,
                    slope_r2=None,
                    vol_expand_sec=None,
                    vol_expand_before_safety=None,
                    vol_peak_sec=None,
                )
            )
            continue

        # Arrays
        ts = w["timestamp"]
        rets = w["ret"].to_numpy(dtype=float)
        vols = w["notional"].to_numpy(dtype=float)
        tsec = (ts - ts0).dt.total_seconds().to_numpy(dtype=float)

        # Core forward-shape metrics
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
        vol_peak_sec = int(round(float(tsec[vol_peak_idx]))) if len(tsec) else None

        # coarse 5-bucket paths (for clustering later; do not bucket the *trade* itself)
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

        # --- Advanced forward metrics (existing) ---
        if len(rets):
            ru_idx = time_to_max_runup
            if ru_idx > 0:
                dd_before = float(np.minimum.accumulate(rets[: ru_idx + 1]).min())
            else:
                dd_before = 0.0

            above = int(np.count_nonzero(rets > 0.0))
            below = int(np.count_nonzero(rets < 0.0))
            total = int(len(rets))
            frac_above = above / total if total > 0 else 0.0
            frac_below = below / total if total > 0 else 0.0
        else:
            dd_before = 0.0
            frac_above = 0.0
            frac_below = 0.0

        if max_drawdown < 0.0:
            ru_over_dd = max_runup / abs(max_drawdown) if abs(max_drawdown) > 1e-6 else 0.0
        else:
            ru_over_dd = 0.0

        # ------------------------------------------------------------
        # Trade-like shape metrics (your "non-negotiables")
        # ------------------------------------------------------------

        # Time to first profit (first ret > 0)
        tfp_sec = None
        idx_tfp = np.where(rets > 0.0)[0]
        if len(idx_tfp):
            tfp_sec = int(round(float(tsec[int(idx_tfp[0])])))

        # Time to safety (first ret >= safety)
        tts_sec = None
        idx_safe = np.where(rets >= safe_ret_val)[0]
        safe_i = None
        if len(idx_safe):
            safe_i = int(idx_safe[0])
            tts_sec = int(round(float(tsec[safe_i])))

        # Exit: first hit of target/stop else timeout at horizon
        exit_i = len(rets) - 1
        exit_reason = "timeout"
        hit_t = np.where(rets >= target_ret)[0]
        hit_s = np.where(rets <= stop_ret)[0]
        cand = []
        if len(hit_t):
            cand.append((int(hit_t[0]), "target"))
        if len(hit_s):
            cand.append((int(hit_s[0]), "stop"))
        if cand:
            exit_i, exit_reason = min(cand, key=lambda x: x[0])

        realized_r = float(rets[exit_i]) if len(rets) else 0.0
        clut_sec = int(round(float(tsec[exit_i]))) if len(tsec) else 0
        rpm = (realized_r / (clut_sec / 60.0)) if clut_sec > 0 else None

        # Early MAE (before safety)
        if safe_i is not None:
            early_slice = rets[: safe_i + 1]
        else:
            early_slice = rets[: exit_i + 1]
        early_mae_to_safety = float(np.min(early_slice)) if len(early_slice) else None

        # Time underwater (before safety)
        time_underwater = None
        if len(tsec) >= 2:
            end_i = safe_i if safe_i is not None else exit_i
            end_i = min(end_i, len(rets) - 1)
            uw = 0.0
            # integrate time where ret < 0 using left-hand value per segment
            for i in range(0, end_i):
                dt = float(tsec[i + 1] - tsec[i])
                if dt < 0:
                    continue
                if rets[i] < 0.0:
                    uw += dt
            time_underwater = int(round(uw))

        # Profit quality
        mfe = max_runup if max_runup > 0 else None
        mae = max_drawdown if max_drawdown < 0 else None
        efficiency = (realized_r / mfe) if (mfe is not None and abs(mfe) > 1e-9) else None
        mae_over_mfe = (abs(mae) / mfe) if (mfe is not None and mae is not None and abs(mfe) > 1e-9) else None

        # Direction changes (chop proxy)
        direction_changes = None
        if len(rets) >= 3:
            d = np.diff(rets)
            eps = 0.0002  # 2 bps: ignore microscopic jiggle
            signs = np.sign(d)
            signs[np.abs(d) < eps] = 0
            # compress zeros
            non0 = [int(s) for s in signs if s != 0]
            if len(non0) >= 2:
                direction_changes = sum(1 for a, b in zip(non0, non0[1:]) if a != b)
            else:
                direction_changes = 0

        # Slope consistency (trendiness) using simple linear regression on ret vs seconds
        slope_per_min = None
        slope_r2 = None
        if len(rets) >= 5 and len(tsec) == len(rets):
            x = tsec[: exit_i + 1]
            y = rets[: exit_i + 1]
            x0 = x - float(x.mean())
            denom = float((x0 ** 2).sum())
            if denom > 1e-9:
                b = float((x0 * (y - float(y.mean()))).sum() / denom)  # ret per second
                slope_per_min = b * 60.0
                y_hat = float(y.mean()) + b * (x - float(x.mean()))
                ss_res = float(((y - y_hat) ** 2).sum())
                ss_tot = float(((y - float(y.mean())) ** 2).sum())
                slope_r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 1e-12 else None

        # Volatility expansion timing (simple: first time notional > 2x rolling median baseline)
        vol_expand_sec = None
        vol_expand_before_safety = None
        if len(vols) >= 10 and len(tsec) == len(vols):
            # baseline: median of first 30 seconds (or first 20% of window)
            baseline_mask = tsec <= min(30.0, float(tsec[-1]) * 0.2)
            base = float(np.median(vols[baseline_mask])) if baseline_mask.any() else float(np.median(vols))
            thr = base * 2.0
            idx = np.where(vols >= thr)[0]
            if len(idx):
                vol_expand_sec = int(round(float(tsec[int(idx[0])])))
                if tts_sec is not None:
                    vol_expand_before_safety = bool(vol_expand_sec <= tts_sec)

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
                dd_before_runup=dd_before,
                ru_over_dd=ru_over_dd,
                frac_above_entry=frac_above,
                frac_below_entry=frac_below,
                safety_ret=safe_ret_val,
                tts_sec=tts_sec,
                early_mae_to_safety=early_mae_to_safety,
                time_underwater_to_safety=time_underwater,
                tfp_sec=tfp_sec,
                clut_sec=clut_sec,
                exit_reason=exit_reason,
                realized_r=realized_r,
                rpm=rpm,
                efficiency=efficiency,
                mae_over_mfe=mae_over_mfe,
                direction_changes=direction_changes,
                slope_per_min=slope_per_min,
                slope_r2=slope_r2,
                vol_expand_sec=vol_expand_sec,
                vol_expand_before_safety=vol_expand_before_safety,
                vol_peak_sec=vol_peak_sec,
            )
        )

    return out


# ------------------------------------------------------------
# DB WRITE HELPERS
# ------------------------------------------------------------

def _clean_for_json(obj):
    """Recursively replace NaN/inf floats with None so JSON/JSONB will accept them."""
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
    """
    pattern_forward_shape gets the "core" forward metrics only.

    Core shape/trade metrics live in the JSON payload of pattern_feature_record.
    """
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
    entry_kind: str,
    parent_event_ts: datetime | None,
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

            # tagging
            "entry_kind": entry_kind,
            "parent_event_ts": parent_event_ts.isoformat() if parent_event_ts else None,

            # existing advanced metrics
            "dd_before_runup": s.dd_before_runup,
            "ru_over_dd": s.ru_over_dd,
            "frac_above_entry": s.frac_above_entry,
            "frac_below_entry": s.frac_below_entry,
            # trade lifecycle / core shape metrics
            "safety_ret": s.safety_ret,
            "tts_sec": s.tts_sec,
            "early_mae_to_safety": s.early_mae_to_safety,
            "time_underwater_to_safety": s.time_underwater_to_safety,
            "tfp_sec": s.tfp_sec,
            "clut_sec": s.clut_sec,
            "exit_reason": s.exit_reason,
            "realized_r": s.realized_r,
            "rpm": s.rpm,
            "efficiency": s.efficiency,
            "mae_over_mfe": s.mae_over_mfe,
            "direction_changes": s.direction_changes,
            "slope_per_min": s.slope_per_min,
            "slope_r2": s.slope_r2,
            "vol_expand_sec": s.vol_expand_sec,
            "vol_expand_before_safety": s.vol_expand_before_safety,
            "vol_peak_sec": s.vol_peak_sec,        }
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

    events = find_ross_bull_flag_events(df, symbol)
    if not events:
        return

    # NOTE: we don't dedupe overlapping events yet. If this becomes noisy,
    # we can add a simple cooldown window (e.g., 60-120s) later.
    for ev in events:
        shapes = compute_forward_shapes(df, ev.index, horizons=horizons)
        if not shapes:
            continue

        upsert_pattern_hit(conn, ev)
        for s in shapes:
            upsert_forward_shape(conn, ev.symbol, ev.event_ts, ev.pattern_name, s)
        insert_feature_record(conn, ev.symbol, ev.event_ts, ev.pattern_name, ev.price_event, shapes, ev.entry_kind, ev.parent_event_ts)

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
    parquet_root = resolve_parquet_root()
    dsn = resolve_pg_dsn()

    print(f"[INFO] PARQUET_ROOT={parquet_root}")
    # print only host/db part (avoid leaking credentials)
    print(f"[INFO] PG_DSN={dsn.split('@')[-1]}")

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
        "  python pts_ross_bull_flag.py SYMBOL_OR_PATTERN START [END]\n"
        "\n"
        "Examples:\n"
        "  # one symbol, one day\n"
        "  pts_ross_bull_flag KROS 2025-08-01\n"
        "\n"
        "  # one symbol, range inclusive\n"
        "  pts_ross_bull_flag KROS 2025-08-01 2025-12-05\n"
        "\n"
        "  # all symbols, one day\n"
        "  pts_ross_bull_flag ALL 2025-11-25\n"
        "\n"
        "  # wildcard pattern, range\n"
        "  pts_ross_bull_flag K* 2025-08-01 2025-08-31\n"
        "\n"
        "Env:\n"
        "  REFLEX_PG_DSN, REFLEX_STORAGE_PARQUET_ROOT or PARQUET_ROOT,\n"
        "  REFLEX_RUN_ID (optional)\n"
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

    # Horizons tuned for Ross-style intraday scalps (trade-like view uses horizon as timeout)
    horizons = (1, 3, 5, 10)

    parquet_root = resolve_parquet_root()
    symbols = resolve_symbol_pattern(parquet_root, symbol_pattern)

    if not symbols:
        print(f"[WARN] No symbols matched pattern '{symbol_pattern}' under {parquet_root}")
        return

    instance = os.getenv("REFLEX_INSTANCE_ID", "liveA")

    print(f"[INFO] RUN_ID={RUN_ID}")
    print(f"[INFO] INSTANCE={instance}")
    print(f"[INFO] PARQUET_ROOT={parquet_root}")
    print(f"[INFO] Symbols: {', '.join(symbols)}")
    print(f"[INFO] Dates: {start} .. {end} (inclusive)")
    print(f"[INFO] Pattern: ross_bull_flag")

    run_for_symbols_and_range(symbols, start, end, horizons=horizons)


if __name__ == "__main__":
    main()
