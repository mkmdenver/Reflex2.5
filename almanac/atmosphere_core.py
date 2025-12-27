"""
atmosphere_core.py

Shared compute + DB helpers for the Almanac subsystem.

Canonical storage:
- breadth_1m(ts, scope, metric, value, labels)
  PK(ts, scope, metric) so repeated runs must manage overwrite intentionally.

Env (must match repo .env):
- REFLEX_PG_DSN (preferred)
- PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE (fallback)

Realtime-safety discipline:
- Metrics at time T use only the CLOSED 1m bar at T (historical = already closed).
- No future-looking day high/low, no day-end volume, etc.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd
import psycopg
from psycopg.rows import dict_row
from zoneinfo import ZoneInfo


NY = ZoneInfo("America/New_York")


# -----------------------------
# Tiny .env loader
# -----------------------------
def load_dotenv(dotenv_path: str) -> None:
    if not dotenv_path or not os.path.exists(dotenv_path):
        return
    with open(dotenv_path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and (k not in os.environ):
                os.environ[k] = v


def find_repo_root(start_dir: str) -> str:
    """
    Walk up until we find a marker: .env or requirements.txt
    """
    cur = os.path.abspath(start_dir)
    for _ in range(8):
        if os.path.exists(os.path.join(cur, ".env")) or os.path.exists(os.path.join(cur, "requirements.txt")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.abspath(start_dir)


# -----------------------------
# Postgres
# -----------------------------
def connect_pg() -> psycopg.Connection:
    dsn = os.getenv("REFLEX_PG_DSN")  # match your .env naming
    if not dsn:
        host = os.getenv("PGHOST", "localhost")
        port = int(os.getenv("PGPORT", "5432"))
        user = os.getenv("PGUSER", "postgres")
        password = os.getenv("PGPASSWORD", "")
        db = os.getenv("PGDATABASE", "stock_data")
        dsn = f"postgresql://{user}:{password}@{host}:{port}/{db}"
    return psycopg.connect(dsn, row_factory=dict_row)


# -----------------------------
# Time / sessions
# -----------------------------
def parse_ymd(s: str) -> date:
    return date.fromisoformat(s)


def daterange(d0: date, d1: date) -> Iterable[date]:
    d = d0
    while d <= d1:
        yield d
        d += timedelta(days=1)


def day_bounds_utc(d: date) -> Tuple[datetime, datetime]:
    start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def ny_session_segment(ts_utc: pd.Timestamp) -> Optional[str]:
    """
    PRE 04:00-09:30 ET, RTH 09:30-16:00 ET, AH 16:00-20:00 ET.
    Returns None outside those hours.
    """
    ts_ny = ts_utc.tz_convert(NY)
    hm = ts_ny.hour * 60 + ts_ny.minute
    pre_start = 4 * 60
    rth_start = 9 * 60 + 30
    rth_end = 16 * 60
    ah_end = 20 * 60
    if pre_start <= hm < rth_start:
        return "PRE"
    if rth_start <= hm < rth_end:
        return "RTH"
    if rth_end <= hm < ah_end:
        return "AH"
    return None


def ny_tod_key(ts_utc: pd.Timestamp) -> str:
    ts_ny = ts_utc.tz_convert(NY)
    return f"{ts_ny.hour:02d}:{ts_ny.minute:02d}"


# -----------------------------
# Data loading
# -----------------------------
def load_minutes(
    conn: psycopg.Connection,
    symbols: List[str],
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame(columns=["symbol", "timestamp", "close", "volume"])
    q = """
    SELECT symbol, timestamp, close, volume
    FROM minute_bars
    WHERE timestamp >= %s AND timestamp < %s
      AND symbol = ANY(%s)
    ORDER BY timestamp, symbol
    """
    with conn.cursor() as cur:
        cur.execute(q, (start, end, symbols))
        rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(columns=["symbol", "timestamp", "close", "volume"])
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype(np.int64)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df


def fetch_symbols_for_range(conn: psycopg.Connection, start: datetime, end: datetime) -> List[str]:
    q = """
    SELECT DISTINCT symbol
    FROM minute_bars
    WHERE timestamp >= %s AND timestamp < %s
    ORDER BY symbol
    """
    with conn.cursor() as cur:
        cur.execute(q, (start, end))
        return [r["symbol"] for r in cur.fetchall()]


def select_symbols(all_syms: List[str], target: str) -> List[str]:
    """
    target:
      ALL or *
      wildcard (K*, *BIO*)
      single symbol (KROS)
    """
    t = target.strip().upper()
    if t in ("ALL", "*"):
        return all_syms

    # glob-like wildcard
    if any(ch in t for ch in ["*", "?", "[", "]"]):
        import fnmatch
        return [s for s in all_syms if fnmatch.fnmatch(s.upper(), t)]

    return [t]


# -----------------------------
# Baseline (fixed for run, v1)
# -----------------------------
def build_fixed_baseline(
    conn: psycopg.Connection,
    symbols: List[str],
    start_date: date,
    lookback_days: int,
) -> pd.DataFrame:
    """
    Baseline: per-symbol, per-session-segment, per-minute-of-day median volume.
    Uses ONLY days before start_date (no lookahead).
    """
    if not symbols:
        return pd.DataFrame(columns=["symbol", "segment", "tod", "median_vol"])

    lb_start = start_date - timedelta(days=lookback_days * 2)
    lb_end = start_date  # exclusive
    q_start = datetime(lb_start.year, lb_start.month, lb_start.day, tzinfo=timezone.utc)
    q_end = datetime(lb_end.year, lb_end.month, lb_end.day, tzinfo=timezone.utc)

    df = load_minutes(conn, symbols, q_start, q_end)
    if df.empty:
        return pd.DataFrame(columns=["symbol", "segment", "tod", "median_vol"])

    ts = pd.to_datetime(df["timestamp"], utc=True)
    df["segment"] = ts.apply(ny_session_segment)
    df = df[df["segment"].notna()].copy()
    if df.empty:
        return pd.DataFrame(columns=["symbol", "segment", "tod", "median_vol"])

    df["tod"] = ts.apply(ny_tod_key)

    baseline = (
        df.groupby(["symbol", "segment", "tod"], as_index=False)["volume"]
        .median()
        .rename(columns={"volume": "median_vol"})
    )
    return baseline


def attach_rvol_self(df: pd.DataFrame, baseline: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        df["rvol_self"] = np.nan
        return df

    ts = pd.to_datetime(df["timestamp"], utc=True)
    out = df.copy()
    out["segment"] = ts.apply(ny_session_segment)
    out = out[out["segment"].notna()].copy()
    if out.empty:
        out["rvol_self"] = np.nan
        return out

    out["tod"] = ts.apply(ny_tod_key)
    out = out.merge(baseline, how="left", on=["symbol", "segment", "tod"])
    out["rvol_self"] = out["volume"] / out["median_vol"].replace({0: np.nan})
    return out


def compute_ret_close(close: pd.Series, periods: int) -> pd.Series:
    prev = close.shift(periods)
    return (close / prev) - 1.0


# -----------------------------
# breadth_1m writes + clearing
# -----------------------------

def upsert_breadth_rows(
    conn: psycopg.Connection,
    rows: List[Tuple[datetime, str, str, float, Optional[dict]]],
) -> None:
    if not rows:
        return

    # psycopg3 needs explicit JSON adaptation for dict -> jsonb
    from psycopg.types.json import Json

    q = """
    INSERT INTO breadth_1m (ts, scope, metric, value, labels)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (ts, scope, metric)
    DO UPDATE SET
        value  = EXCLUDED.value,
        labels = COALESCE(EXCLUDED.labels, breadth_1m.labels)
    """

    adapted = []
    for ts, scope, metric, value, labels in rows:
        adapted.append((ts, scope, metric, value, Json(labels) if labels is not None else None))

    with conn.cursor() as cur:
        cur.executemany(q, adapted)
    conn.commit()


def delete_breadth_range(
    conn: psycopg.Connection,
    start_ts: datetime,
    end_ts: datetime,
    scope_prefix: Optional[str] = None,
) -> int:
    """
    Deletes breadth_1m rows in [start_ts, end_ts) optionally filtered by scope prefix.
    """
    if scope_prefix:
        q = """
        DELETE FROM breadth_1m
        WHERE ts >= %s AND ts < %s
          AND scope LIKE %s
        """
        args = (start_ts, end_ts, f"{scope_prefix}%")
    else:
        q = """
        DELETE FROM breadth_1m
        WHERE ts >= %s AND ts < %s
        """
        args = (start_ts, end_ts)

    with conn.cursor() as cur:
        cur.execute(q, args)
        n = cur.rowcount
    conn.commit()
    return int(n)


# -----------------------------
# Metric emission (v1)
# -----------------------------
@dataclass
class AlmanacConfig:
    target: str
    start_date: date
    end_date: date
    lookback_days: int = 30

    # logical scope naming
    universe_name: str = "ALL"

    # metrics
    hot_rvol_threshold: float = 2.0
    write_spy: bool = True

    # storage
    run_id: str = "dev"
    mode: str = "overwrite"  # overwrite|append


def generate_day(
    conn: psycopg.Connection,
    cfg: AlmanacConfig,
    symbols_for_universe: List[str],
    symbols_for_compute: List[str],
    baseline: pd.DataFrame,
    d: date,
) -> None:
    d0, d1 = day_bounds_utc(d)
    df = load_minutes(conn, symbols_for_compute, d0, d1)
    if df.empty:
        return

    df = attach_rvol_self(df, baseline)
    if df.empty:
        return

    scope_universe = f"universe:{cfg.universe_name}"
    labels_common = {"run_id": cfg.run_id, "gen": "almanac_history", "v": "1.0"}

    # Universe group metrics per closed minute
    # IMPORTANT: group should be based on the selected universe set, not necessarily SPY add-ons
    df_u = df[df["symbol"].isin(symbols_for_universe)].copy()
    if not df_u.empty:
        grp = df_u.groupby("timestamp", as_index=False).agg(
            group_count=("symbol", "count"),
            group_median_rvol=("rvol_self", "median"),
            group_hot_frac=("rvol_self", lambda x: float(np.mean(x >= cfg.hot_rvol_threshold))),
        )

        rows: List[Tuple[datetime, str, str, float, Optional[dict]]] = []
        for _, r in grp.iterrows():
            ts = pd.Timestamp(r["timestamp"]).to_pydatetime()
            rows.append((ts, scope_universe, "group_count", float(r["group_count"]), labels_common))
            if pd.notna(r["group_median_rvol"]):
                rows.append((ts, scope_universe, "group_median_rvol_self", float(r["group_median_rvol"]), labels_common))
            if pd.notna(r["group_hot_frac"]):
                rows.append(
                    (
                        ts,
                        scope_universe,
                        "group_hot_frac_rvol_ge_2",
                        float(r["group_hot_frac"]),
                        {**labels_common, "thr": cfg.hot_rvol_threshold},
                    )
                )

        upsert_breadth_rows(conn, rows)

    # Optional: SPY returns (close-to-close), if SPY is present in minute_bars
    if cfg.write_spy and ("SPY" in df["symbol"].unique()):
        spy = df[df["symbol"] == "SPY"].sort_values("timestamp")
        if not spy.empty:
            close = spy["close"]
            ts = spy["timestamp"]

            r5 = compute_ret_close(close, 5)
            r15 = compute_ret_close(close, 15)

            rows2: List[Tuple[datetime, str, str, float, Optional[dict]]] = []
            for t, v in zip(ts, r5):
                if pd.notna(v):
                    rows2.append((pd.Timestamp(t).to_pydatetime(), "etf:SPY", "ret_5m_close", float(v), labels_common))
            for t, v in zip(ts, r15):
                if pd.notna(v):
                    rows2.append((pd.Timestamp(t).to_pydatetime(), "etf:SPY", "ret_15m_close", float(v), labels_common))
            upsert_breadth_rows(conn, rows2)
