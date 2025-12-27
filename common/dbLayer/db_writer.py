# common/db_writer.py
"""
High-level DB writers for Reflex using psycopg v3.

- Depends on common.dbutils (psycopg v3) and its bulk_upsert().
- Provides production-grade upsert helpers for daily, minute, tick, and quote data.
- Accepts Polygon-style rows or DB-style rows; maps to DB columns.
- Adapts to schema via environment variables (no code edits for naming differences).

Environment overrides (optional):
  # tables
  REFLEX_TABLE_DAILY              default "daily_bars"
  REFLEX_TABLE_MINUTE             default "minute_bars"
  REFLEX_TABLE_TICK               default "tick_data"
  REFLEX_TABLE_QUOTE              default "quote_data"

  # shared
  REFLEX_COL_SYMBOL               default "symbol"

  # daily bars
  REFLEX_COL_DAILY_TS             default "timestamp"   (DATE in your schema)
  REFLEX_COL_OPEN                 default "open"
  REFLEX_COL_HIGH                 default "high"
  REFLEX_COL_LOW                  default "low"
  REFLEX_COL_CLOSE                default "close"
  REFLEX_COL_VOLUME               default "volume"

  # minute bars
  REFLEX_COL_MINUTE_TS            default "timestamp"   (TIMESTAMPTZ)

  # ticks
  REFLEX_COL_TICK_TS              default "timestamp"         (TIMESTAMPTZ)
  REFLEX_COL_SIP_TICK_TS          default "sip_timestamp"     (BIGINT, NOT NULL)
  REFLEX_COL_PRICE                default "price"
  REFLEX_COL_SIZE                 default "size"
  REFLEX_COL_EXCHANGE             default "exchange"
  REFLEX_COL_CONDITIONS           default "conditions"        (TEXT[] recommended)

  # quotes
  REFLEX_COL_QUOTE_TS             default "ts"          (TIMESTAMPTZ)
  REFLEX_COL_BID_PRICE            default "bid_price"
  REFLEX_COL_BID_SIZE             default "bid_size"
  REFLEX_COL_ASK_PRICE            default "ask_price"
  REFLEX_COL_ASK_SIZE             default "ask_size"
  REFLEX_COL_BID_EXCHANGE         default "bid_exchange"
  REFLEX_COL_ASK_EXCHANGE         default "ask_exchange"
  REFLEX_COL_QUOTE_CONDITIONS     default "conditions"  (TEXT[])
"""

from __future__ import annotations

import os
import sys
import math
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

import pandas as pd
from .dbutils import bulk_upsert

# ------------------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------------------
import logging
log = logging.getLogger("db_layer/db_writer")
if not log.handlers:
    logging.basicConfig(level=logging.INFO)
log.setLevel(logging.INFO)

# ------------------------------------------------------------------------------
# Bootstrap sys.path (works from batch or `python -m`)
# ------------------------------------------------------------------------------
_THIS_DIR = os.path.dirname(__file__)
_PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, os.pardir))
_COMMON_DIR = os.path.join(_PROJECT_ROOT, "common")
for path in [_PROJECT_ROOT, _COMMON_DIR]:
    if path not in sys.path:
        sys.path.insert(0, path)

# ------------------------------------------------------------------------------
# Table / Column configuration (env overrides)
# ------------------------------------------------------------------------------
TABLE_DAILY   = os.getenv("REFLEX_TABLE_DAILY",   "daily_bars")
TABLE_MINUTE  = os.getenv("REFLEX_TABLE_MINUTE",  "minute_bars")
TABLE_TICK    = os.getenv("REFLEX_TABLE_TICK",    "tick_data")
TABLE_QUOTE   = os.getenv("REFLEX_TABLE_QUOTE",   "quote_data")

COL_SYMBOL    = os.getenv("REFLEX_COL_SYMBOL",    "symbol")

# Bars: date & timestamp
COL_DAILY_TS  = os.getenv("REFLEX_COL_DAILY_TS",  "timestamp")   # DATE
COL_MINUTE_TS = os.getenv("REFLEX_COL_MINUTE_TS", "timestamp")   # TIMESTAMPTZ

# OHLCV columns
COL_OPEN      = os.getenv("REFLEX_COL_OPEN",      "open")
COL_HIGH      = os.getenv("REFLEX_COL_HIGH",      "high")
COL_LOW       = os.getenv("REFLEX_COL_LOW",       "low")
COL_CLOSE     = os.getenv("REFLEX_COL_CLOSE",     "close")
COL_VOLUME    = os.getenv("REFLEX_COL_VOLUME",    "volume")

# Tick columns (sip_timestamp is REQUIRED by your DB unique/NOT NULL)
COL_TICK_TS   = os.getenv("REFLEX_COL_TICK_TS",       "timestamp")
COL_SIP_TS    = os.getenv("REFLEX_COL_SIP_TICK_TS",   "sip_timestamp")
COL_PRICE     = os.getenv("REFLEX_COL_PRICE",         "price")
COL_SIZE      = os.getenv("REFLEX_COL_SIZE",          "size")
COL_EXCHANGE  = os.getenv("REFLEX_COL_EXCHANGE",      "exchange")
COL_TK_COND   = os.getenv("REFLEX_COL_CONDITIONS",    "conditions")

# Quote columns
COL_QUOTE_TS  = os.getenv("REFLEX_COL_QUOTE_TS",  "ts")
COL_BID_PRICE = os.getenv("REFLEX_COL_BID_PRICE", "bid_price")
COL_BID_SIZE  = os.getenv("REFLEX_COL_BID_SIZE",  "bid_size")
COL_ASK_PRICE = os.getenv("REFLEX_COL_ASK_PRICE", "ask_price")
COL_ASK_SIZE  = os.getenv("REFLEX_COL_ASK_SIZE",  "ask_size")
COL_BID_EXCH  = os.getenv("REFLEX_COL_BID_EXCHANGE", "bid_exchange")
COL_ASK_EXCH  = os.getenv("REFLEX_COL_ASK_EXCHANGE", "ask_exchange")
COL_QT_COND   = os.getenv("REFLEX_COL_QUOTE_CONDITIONS", "conditions")

# Optional: allow synthesizing sip_timestamp from wall-clock ts when missing
ALLOW_SYNTH_NS = os.getenv("REFLEX_TICKS_ALLOW_SYNTH_NS", "0") == "1"

# ------------------------------------------------------------------------------
# Time helpers
# ------------------------------------------------------------------------------
def _epoch_to_dt_utc(t: int | float) -> datetime:
    """
    Convert epoch seconds/milliseconds/nanoseconds to UTC datetime.
    """
    if t is None:
        raise ValueError("Missing epoch timestamp value")
    # Heuristics on magnitude
    if t > 1e14:          # ns
        seconds = t / 1e9
    elif t > 1e11:        # ms
        seconds = t / 1e3
    else:                 # s
        seconds = t
    return datetime.fromtimestamp(seconds, tz=timezone.utc)

def _minute_floor(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)

# ------------------------------------------------------------------------------
# Small utils
# ------------------------------------------------------------------------------
def _is_nan(x: Any) -> bool:
    return isinstance(x, float) and math.isnan(x)

def _first_ns_timestamp(row: Mapping[str, Any]) -> Optional[int]:
    """
    Return the first non-null *nanosecond* timestamp available in the row.
    Preference order: sip_timestamp, participant_timestamp, trf_timestamp.
    Accepts common aliases used upstream: 'sip', 'participant_ts', 'trf_ts'.
    """
    candidates = (
        COL_SIP_TS, "sip_timestamp", "sip",
        "participant_timestamp", "participant_ts",
        "trf_timestamp", "trf_ts",
    )
    for key in candidates:
        if key in row:
            v = row.get(key)
            if v is None or _is_nan(v):
                continue
            try:
                return int(v)
            except Exception:
                # Some sources send strings/floats
                try:
                    return int(float(v))
                except Exception:
                    continue
    return None

def _to_text_array(val: Any) -> Optional[List[str]]:
    """
    Normalize Polygon 'conditions' to a Postgres text[] (or None).
    Accepts: list/tuple/set of ints/strs, a single int/float/str, or NaN/None.
    """
    if val is None or _is_nan(val):
        return None

    # If it's already a list/tuple/set, coerce members to str and drop empties/NaN.
    if isinstance(val, (list, tuple, set)):
        out: List[str] = []
        for v in val:
            if v is None or _is_nan(v):
                continue
            if isinstance(v, int):
                out.append(str(v))
            elif isinstance(v, float):
                out.append(str(int(v)) if v.is_integer() else str(v))
            else:
                out.append(str(v))
        return out if out else None

    # Single value
    if isinstance(val, int):
        return [str(val)]
    if isinstance(val, float):
        if _is_nan(val):
            return None
        return [str(int(val)) if val.is_integer() else str(val)]
    return [str(val)]

# ------------------------------------------------------------------------------
# Mapping helpers (accept Polygon-style or DB-native rows)
# ------------------------------------------------------------------------------
def _map_daily_row(row: Mapping[str, Any], *, symbol: str) -> Dict[str, Any]:
    if not isinstance(row, dict):
        raise TypeError(f"[❌] Expected dict for row, got {type(row).__name__}: {repr(row)}")

    o = row.get(COL_OPEN,   row.get("o"))
    h = row.get(COL_HIGH,   row.get("h"))
    l = row.get(COL_LOW,    row.get("l"))
    c = row.get(COL_CLOSE,  row.get("c"))
    v = row.get(COL_VOLUME, row.get("v"))

    if None in (o, h, l, c, v):
        missing = [k for k, v_ in [(COL_OPEN,o),(COL_HIGH,h),(COL_LOW,l),(COL_CLOSE,c),(COL_VOLUME,v)] if v_ is None]
        raise ValueError(f"[❌] Daily bar row missing fields: {missing} → {row}")

    # Resolve date
    if COL_DAILY_TS in row:
        day_val = row[COL_DAILY_TS]
    elif "day" in row:
        day_val = row["day"]
    elif "t" in row:
        day_val = _epoch_to_dt_utc(row["t"]).date()
    elif "ts_utc" in row:
        day_val = pd.to_datetime(row["ts_utc"]).date()
    else:
        raise ValueError(f"[❌] Daily bar row missing {COL_DAILY_TS}/'day'/'t'/'ts_utc': {row}")

    out = {
        COL_SYMBOL:   symbol,
        COL_DAILY_TS: day_val,
        COL_OPEN:     o,
        COL_HIGH:     h,
        COL_LOW:      l,
        COL_CLOSE:    c,
        COL_VOLUME:   v,
    }
    
    return out

def _map_minute_row(row: Mapping[str, Any], *, symbol: str) -> Dict[str, Any]:
    o = row.get(COL_OPEN,   row.get("o"))
    h = row.get(COL_HIGH,   row.get("h"))
    l = row.get(COL_LOW,    row.get("l"))
    c = row.get(COL_CLOSE,  row.get("c"))
    v = row.get(COL_VOLUME, row.get("v"))

    if None in (o, h, l, c, v):
        missing = [k for k, v_ in [(COL_OPEN,o),(COL_HIGH,h),(COL_LOW,l),(COL_CLOSE,c),(COL_VOLUME,v)] if v_ is None]
        raise ValueError(f"Minute bar row missing fields: {missing}")

    # Resolve timestamp, floor to minute
    if COL_MINUTE_TS in row:
        ts_val = row[COL_MINUTE_TS]
    elif "ts" in row:
        ts_val = row["ts"]
    elif "t" in row:
        ts_val = _minute_floor(_epoch_to_dt_utc(row["t"]))
    elif "ts_utc" in row:
        ts_val = _minute_floor(pd.to_datetime(row["ts_utc"]))
    else:
        raise ValueError(f"Minute bar row missing {COL_MINUTE_TS}/'ts'/'t'/'ts_utc'")

    out = {
        COL_SYMBOL:   symbol,
        COL_MINUTE_TS: ts_val,
        COL_OPEN:     o,
        COL_HIGH:     h,
        COL_LOW:      l,
        COL_CLOSE:    c,
        COL_VOLUME:   v,
    }
    return out

def _map_tick_row(row: Mapping[str, Any], *, symbol: str) -> Dict[str, Any]:
    """
    Map a tick row. Enforces presence of a *nanosecond* unique id suitable
    for DB unique/NOT NULL (sip -> participant -> trf -> optional synth).
    Normalizes conditions to text[] and ALWAYS includes the conditions key (possibly None).
    """
    if not isinstance(row, dict):
        raise TypeError(f"Expected dict for tick row, got {type(row)}: {repr(row)}")

    # Resolve wall-clock timestamp for DB storage
    if COL_TICK_TS in row:
        ts_val = row[COL_TICK_TS]
    elif "ts" in row:
        ts_val = row["ts"]
    elif "ts_utc" in row:
        ts_val = pd.to_datetime(row["ts_utc"])
    elif "t" in row:
        ts_val = _epoch_to_dt_utc(row["t"])
    else:
        print("Failing row (no ts):", row)
        raise ValueError("Tick row missing timestamp/ts/ts_utc/t")

    price = row.get(COL_PRICE, row.get("p"))
    size  = row.get(COL_SIZE,  row.get("s"))
    if price is None or size is None:
        missing = [k for k, v_ in [(COL_PRICE,price),(COL_SIZE,size)] if v_ is None]
        raise ValueError(f"Tick row missing fields: {missing}")

    # Nanosecond unique id: sip -> participant -> trf
    sip_ns = row.get(COL_SIP_TS, row.get("sip_timestamp"))
    if sip_ns is None:
        sip_ns = _first_ns_timestamp(row)
    if sip_ns is None and ALLOW_SYNTH_NS:
        try:
            sip_ns = int(pd.to_datetime(ts_val, utc=True).value)  # ns since epoch
        except Exception:
            pass
    if sip_ns is None:
        return {"__DROP__": True, COL_SYMBOL: symbol, COL_TICK_TS: ts_val}

    exch = row.get(COL_EXCHANGE, row.get("x"))
    cond = _to_text_array(row.get(COL_TK_COND, row.get("c")))  # normalize → text[] or None

    out: Dict[str, Any] = {
        COL_SYMBOL:  symbol,
        COL_TICK_TS: ts_val,
        COL_SIP_TS:  int(sip_ns),
        COL_PRICE:   price,
        COL_SIZE:    size,
        COL_TK_COND: cond,   # <-- ALWAYS include key, even if None
    }
    if exch is not None:
        out[COL_EXCHANGE] = exch
    return out


def _map_quote_row(row: Mapping[str, Any], *, symbol: str) -> Dict[str, Any]:
    bp   = row.get(COL_BID_PRICE, row.get("bp"))
    bs   = row.get(COL_BID_SIZE,  row.get("bs"))
    ap   = row.get(COL_ASK_PRICE, row.get("ap"))
    asz  = row.get(COL_ASK_SIZE,  row.get("as"))
    bx   = row.get(COL_BID_EXCH,  row.get("bx"))
    ax   = row.get(COL_ASK_EXCH,  row.get("ax"))
    cond = _to_text_array(row.get(COL_QT_COND, row.get("c")))

    if bp is None or bs is None or ap is None or asz is None:
        missing = [k for k, v_ in [(COL_BID_PRICE,bp),(COL_BID_SIZE,bs),(COL_ASK_PRICE,ap),(COL_ASK_SIZE,asz)] if v_ is None]
        raise ValueError(f"Quote row missing fields: {missing}")

    # Resolve timestamp
    if COL_QUOTE_TS in row:
        ts_val = row[COL_QUOTE_TS]
    elif "ts" in row:
        ts_val = row["ts"]
    elif "ts_utc" in row:
        ts_val = pd.to_datetime(row["ts_utc"])
    elif "t" in row:
        ts_val = _epoch_to_dt_utc(row["t"])
    else:
        raise ValueError(f"Quote row missing {COL_QUOTE_TS}/'ts'/'ts_utc'/'t'")

    out: Dict[str, Any] = {
        COL_SYMBOL:   symbol,
        COL_QUOTE_TS: ts_val,
        COL_BID_PRICE: bp,
        COL_BID_SIZE: bs,
        COL_ASK_PRICE: ap,
        COL_ASK_SIZE: asz,
    }
    if bx is not None:
        out[COL_BID_EXCH] = bx
    if ax is not None:
        out[COL_ASK_EXCH] = ax
    if cond is not None:
        out[COL_QT_COND] = cond
    return out

# ------------------------------------------------------------------------------
# Normalizer
# ------------------------------------------------------------------------------
def _normalize(rows: Iterable[Mapping[str, Any]], mapper, *, symbol: str) -> List[Dict[str, Any]]:
    return [mapper(r, symbol=symbol) for r in rows]

# ------------------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------------------
def upsert_daily_bars_for_symbol(
    symbol: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    dsn: Optional[str] = None,
    update_cols: Optional[Sequence[str]] = None,
    chunk_size: int = 1000,
) -> int:
    norm = _normalize(rows, _map_daily_row, symbol=symbol)
    if not norm:
        return 0
    cols = list(norm[0].keys())
    conflict = [COL_SYMBOL, COL_DAILY_TS]
    if update_cols is None:
        update_cols = [c for c in cols if c not in conflict]

    # Keep your debugging print
    #print("bulk_upsert signature:", bulk_upsert.__code__.co_varnames[:bulk_upsert.__code__.co_argcount])

    return bulk_upsert(
        TABLE_DAILY,
        cols,         # rows_or_columns
        conflict,     # conflict_cols
        norm,         # rows
        update_cols,  # update_cols
        dsn,          # dsn
        chunk_size    # chunk_size
        # commit uses default in dbutils.bulk_upsert
    )

def upsert_minute_bars_for_symbol(
    symbol: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    dsn: Optional[str] = None,
    update_cols: Optional[Sequence[str]] = None,
    chunk_size: int = 2000,
) -> int:
    norm = _normalize(rows, _map_minute_row, symbol=symbol)
    if not norm:
        return 0
    cols = list(norm[0].keys())
    conflict = [COL_SYMBOL, COL_MINUTE_TS]
    if update_cols is None:
        update_cols = [c for c in cols if c not in conflict]

    return bulk_upsert(
        TABLE_MINUTE,
        cols,
        conflict,
        norm,
        update_cols,
        dsn,
        chunk_size
    )

def upsert_ticks(
    symbol: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    dsn: Optional[str] = None,
    update_cols: Optional[Sequence[str]] = None,
    chunk_size: int = 5000,
) -> int:
    # Debug visibility for input
    #print(f"upsert_ticks called with type: {type(rows)}")


    norm_all = _normalize(rows, _map_tick_row, symbol=symbol)

    if norm_all:
        sample = norm_all[0]
        #log.info("[ticks] sample keys: %s", list(sample.keys()))
        # Also show a peek at the raw keys
        try:
            first_raw = next(iter(rows))
            #if isinstance(first_raw, dict):
            #    log.info("[ticks] first raw keys: %s", list(first_raw.keys()))
            #else:
            #    log.info("[ticks] first raw type: %s", type(first_raw))
        except Exception:
            pass

    # Drop rows that still lack a valid ns id (mapper marks them with __DROP__)
    norm = [r for r in norm_all if isinstance(r, dict) and not r.get("__DROP__")]
    dropped = len(norm_all) - len(norm)
    if dropped:
        log.warning("[ticks] Dropped %d rows with missing sip/participant/trf timestamp for %s", dropped, symbol)

    if not norm:
        return 0

    cols = list(norm[0].keys())

    # Ensure sip column exists in column list (defensive ordering)
    if COL_SIP_TS not in cols:
        insert_pos = cols.index(COL_TICK_TS) + 1 if COL_TICK_TS in cols else len(cols)
        cols.insert(insert_pos, COL_SIP_TS)

    # Conflict must match DB unique: (symbol, timestamp, sip_timestamp)
    conflict = [COL_SYMBOL, COL_TICK_TS, COL_SIP_TS]

    if update_cols is None:
        update_cols = [c for c in cols if c not in conflict]

    return bulk_upsert(
        TABLE_TICK,
        cols,
        conflict,
        norm,
        update_cols,
        dsn,
        chunk_size
    )

def upsert_quotes_for_symbol(
    symbol: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    dsn: Optional[str] = None,
    update_cols: Optional[Sequence[str]] = None,
    chunk_size: int = 5000,
) -> int:
    norm = _normalize(rows, _map_quote_row, symbol=symbol)
    if not norm:
        return 0
    cols = list(norm[0].keys())
    conflict = [COL_SYMBOL, COL_QUOTE_TS]
    if update_cols is None:
        update_cols = [c for c in cols if c not in conflict]

    return bulk_upsert(
        TABLE_QUOTE,
        cols,
        conflict,
        norm,
        update_cols,
        dsn,
        chunk_size
    )

__all__ = [
    "upsert_daily_bars_for_symbol",
    "upsert_minute_bars_for_symbol",
    "upsert_ticks",
    "upsert_quotes_for_symbol",
]
