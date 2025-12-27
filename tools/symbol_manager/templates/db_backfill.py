# =============================================================================
# dbmanager/db_backfill.py
# Version: 2025.10.22-modes6-selfheal
#
# CHANGELOG
# - 2025-10-22: modes6-selfheal
#   • Self-healing floors: if list_date is NULL, discover earliest bar via Polygon,
#     persist into symbol_metadata.list_date, then clamp all calls to that floor.
#   • Floors logged once per symbol. All mode semantics unchanged.
# =============================================================================

from __future__ import annotations

import logging
import datetime as dt
from typing import Iterable, List, Optional, Tuple, Dict

import psycopg
from psycopg.rows import tuple_row

from common.dbLayer.dbutils import connection, bulk_upsert
from common.polygon_api.rest import (
    fetch_daily_bars,
    fetch_minute_bars_day,
)

# Optional: ticks runner (if present)
try:
    from ticks_backfill import BackfillPlan, run_backfill as run_ticks_backfill
    HAVE_TICKS = True
except Exception:
    HAVE_TICKS = False

LOG = logging.getLogger("DBBackfill")
if not LOG.handlers:
    LOG.addHandler(logging.StreamHandler())
LOG.setLevel(logging.INFO)

# ---------- Policy ------------------------------------------------------------

HARD_MIN_START = dt.date(2004, 1, 1)
MINUTE_FLOOR = dt.date(2021, 1, 1)
FULL_BUFFER_DAYS = 7

RECENT_DAYS_DAILY = 5
RECENT_DAYS_MIN = 3
RECENT_DAYS_TICKS = 1

# ---------- Universe & metadata ----------------------------------------------

def _fetch_all_symbols() -> List[str]:
    sql = "SELECT symbol FROM symbol_metadata ORDER BY symbol"
    try:
        with connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            syms = [r[0] for r in rows]
            if syms:
                LOG.info("[ALL] Using symbol_metadata (all): %d symbols", len(syms))
                return syms
    except Exception as e:
        LOG.error("[ALL] symbol_metadata read failed: %s", e)

    union_sql = """
        SELECT DISTINCT symbol FROM daily_bars
        UNION
        SELECT DISTINCT symbol FROM minute_bars
        ORDER BY 1
    """
    try:
        with connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
            cur.execute(union_sql)
            rows = cur.fetchall()
            syms = [r[0] for r in rows]
            LOG.info("[ALL] Using data tables union: %d symbols", len(syms))
            return syms
    except Exception as e:
        LOG.error("[ALL] fallback read failed: %s", e)
        return []

def _coerce_to_date(val) -> Optional[dt.date]:
    if val is None:
        return None
    if isinstance(val, dt.date) and not isinstance(val, dt.datetime):
        return val
    if isinstance(val, dt.datetime):
        return val.date()
    try:
        return dt.date.fromisoformat(str(val)[:10])
    except Exception:
        return None

def _get_list_date(conn: psycopg.Connection, symbol: str) -> Optional[dt.date]:
    q = "SELECT list_date FROM symbol_metadata WHERE UPPER(symbol) = %s"
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(q, (symbol.upper(),))
        row = cur.fetchone()
        return _coerce_to_date(row[0]) if row else None

def _set_list_date(conn: psycopg.Connection, symbol: str, d: dt.date) -> None:
    up = "UPDATE symbol_metadata SET list_date = %s WHERE UPPER(symbol) = %s"
    with conn.cursor() as cur:
        cur.execute(up, (d, symbol.upper()))
    conn.commit()

# ---------- Self-healing list_date -------------------------------------------

# cache: symbol -> list_date (or None once failed)
_LISTDATE_CACHE: Dict[str, Optional[dt.date]] = {}

def _discover_list_date_via_polygon(symbol: str) -> Optional[dt.date]:
    """
    One-shot discovery: ask Polygon for all daily bars (2004→today),
    take the first bar date as inception. Returns None if truly no data.
    """
    today = dt.datetime.utcnow().date()
    # one call that returns whole history (<= ~8k rows < 50k limit)
    df = fetch_daily_bars(symbol, HARD_MIN_START, today)
    if df is None or getattr(df, "empty", False):
        return None
    first_ts = df["t"].min()
    if first_ts is None:
        return None
    return _coerce_to_date(first_ts.to_pydatetime())

def _ensure_list_date(symbol: str) -> Optional[dt.date]:
    """
    Return a guaranteed best list_date for 'symbol'.
    Strategy:
      1) Cache hit -> return.
      2) Read from DB; if present, cache & return.
      3) Discover via Polygon (earliest daily), persist to DB, cache & return.
      4) If discovery fails, cache None and return None.
    """
    sym = (symbol or "").upper().strip()
    if sym in _LISTDATE_CACHE:
        return _LISTDATE_CACHE[sym]

    with connection() as conn:
        d = _get_list_date(conn, sym)
        if d:
            _LISTDATE_CACHE[sym] = d
            return d

        # Discover once from Polygon and persist
        try:
            d = _discover_list_date_via_polygon(sym)
        except Exception as e:
            LOG.info("[INIT] %s list_date discovery failed: %s", sym, e)
            d = None

        if d:
            try:
                _set_list_date(conn, sym, d)
                LOG.info("[INIT] %s list_date discovered & stored: %s", sym, d)
            except Exception as e:
                LOG.info("[INIT] %s list_date store failed (will use runtime floor only): %s", sym, e)

        _LISTDATE_CACHE[sym] = d
        return d

# ---------- Floors ------------------------------------------------------------

# cache for computed floors
_FLOOR_CACHE: Dict[str, Tuple[dt.date, dt.date, Optional[dt.date]]] = {}

def _symbol_floors(symbol: str) -> Tuple[dt.date, dt.date, Optional[dt.date]]:
    """
    Returns cached (daily_floor, minute_floor, tick_floor) for symbol.
      list_date := ensured (DB or discovered)
      daily_floor  = max(HARD_MIN_START, list_date - FULL_BUFFER_DAYS) if list_date else HARD_MIN_START
      minute_floor = max(MINUTE_FLOOR, list_date)                      if list_date else MINUTE_FLOOR
      tick_floor   = list_date (or None)
    """
    sym = (symbol or "").upper().strip()
    if sym in _FLOOR_CACHE:
        return _FLOOR_CACHE[sym]

    ld = _ensure_list_date(sym)

    if ld:
        daily_floor  = max(HARD_MIN_START, ld - dt.timedelta(days=FULL_BUFFER_DAYS))
        minute_floor = max(MINUTE_FLOOR, ld)
        tick_floor   = ld
    else:
        daily_floor  = HARD_MIN_START
        minute_floor = MINUTE_FLOOR
        tick_floor   = None

    _FLOOR_CACHE[sym] = (daily_floor, minute_floor, tick_floor)
    LOG.info("[FLOOR] %s  daily=%s  minute=%s  tick=%s", sym, daily_floor, minute_floor, tick_floor)
    return daily_floor, minute_floor, tick_floor

# ---------- Date helpers ------------------------------------------------------

def _daterange(d0: dt.date, d1: dt.date) -> Iterable[dt.date]:
    cur = d0
    one = dt.timedelta(days=1)
    while cur <= d1:
        yield cur
        cur += one

def _today() -> dt.date:
    return dt.datetime.utcnow().date()

# ---------- UPSERTS -----------------------------------------------------------

def _upsert_daily(symbol: str, df) -> int:
    if df is None or getattr(df, "empty", False):
        return 0
    df = df.copy()
    if "timestamp" not in df.columns:
        df["timestamp"] = df["t"]
    rename = {}
    if "o" in df.columns: rename["o"] = "open"
    if "h" in df.columns: rename["h"] = "high"
    if "l" in df.columns: rename["l"] = "low"
    if "c" in df.columns: rename["c"] = "close"
    if "v" in df.columns: rename["v"] = "volume"
    df = df.rename(columns=rename)
    for col in ["timestamp","open","high","low","close","volume"]:
        if col not in df.columns:
            raise ValueError(f"daily missing {col}")
    df["symbol"] = symbol
    cols = ["symbol","timestamp","open","high","low","close","volume"]
    conflict = ["symbol","timestamp"]
    updates = ["open","high","low","close","volume"]
    with connection() as conn:
        return bulk_upsert(conn, "daily_bars", df[cols], columns=cols, conflict_cols=conflict, update_cols=updates, chunk_size=2000)

def _upsert_minute(symbol: str, df) -> int:
    if df is None or getattr(df, "empty", False):
        return 0
    df = df.copy()
    if "timestamp" not in df.columns:
        df["timestamp"] = df["t"]
    rename = {}
    if "o" in df.columns: rename["o"] = "open"
    if "h" in df.columns: rename["h"] = "high"
    if "l" in df.columns: rename["l"] = "low"
    if "c" in df.columns: rename["c"] = "close"
    if "v" in df.columns: rename["v"] = "volume"
    df = df.rename(columns=rename)
    for col in ["timestamp","open","high","low","close","volume"]:
        if col not in df.columns:
            raise ValueError(f"minute missing {col}")
    df["symbol"] = symbol
    cols = ["symbol","timestamp","open","high","low","close","volume"]
    conflict = ["symbol","timestamp"]
    updates = ["open","high","low","close","volume"]
    with connection() as conn:
        return bulk_upsert(conn, "minute_bars", df[cols], columns=cols, conflict_cols=conflict, update_cols=updates, chunk_size=5000)

# ---------- Fetchers with floors at call sites --------------------------------

def _do_daily_range(symbol: str, start: dt.date, end: dt.date) -> int:
    daily_floor, _, _ = _symbol_floors(symbol)
    start = max(start, daily_floor)
    if start > end:
        LOG.info("[DLY] %s  %s → %s (clamped empty)", symbol, start, end)
        return 0
    LOG.info("[DLY] %s  %s → %s", symbol, start, end)
    df = fetch_daily_bars(symbol, start, end)
    return _upsert_daily(symbol, df)

def _do_minute_since(symbol: str, start: dt.date, end: dt.date) -> int:
    _, minute_floor, _ = _symbol_floors(symbol)
    start = max(start, minute_floor)
    if start > end:
        LOG.info("[MIN] %s  %s → %s (clamped empty)", symbol, start, end)
        return 0
    total = 0
    for day in _daterange(start, end):
        LOG.info("[MIN] %s  %s", symbol, day)
        df = fetch_minute_bars_day(symbol, day)
        total += _upsert_minute(symbol, df)
    return total

def _do_ticks_range(symbol: str, start: dt.date, end: dt.date) -> None:
    if not HAVE_TICKS:
        LOG.info("[TCK] ticks_backfill module unavailable; skipping")
        return
    _, _, tick_floor = _symbol_floors(symbol)
    if tick_floor is not None:
        start = max(start, tick_floor)
        if start > end:
            LOG.info("[TCK] %s  %s → %s (clamped empty)", symbol, start, end)
            return
    import os, pathlib
    parquet_root = pathlib.Path(os.getenv("PARQUET_ROOT", "") or (os.path.expanduser("~/market")))
    plan = BackfillPlan(
        start=start, end=end, mode="candidates",
        workers=6, force=True, parquet_root=parquet_root,
        log_hook=lambda m: LOG.info("[TCK] %s", m),
        symbols=[symbol] if hasattr(BackfillPlan, "symbols") else None,
    )
    run_ticks_backfill(os.getenv("DATABASE_URL",""), os.getenv("POLYGON_API_KEY",""), plan, lineage=False)

# ---------- Mode runners ------------------------------------------------------

def _recent(symbol: str) -> None:
    today = _today()
    daily_floor, minute_floor, _ = _symbol_floors(symbol)

    dly_start = max(today - dt.timedelta(days=RECENT_DAYS_DAILY), daily_floor)
    _do_daily_range(symbol, dly_start, today)

    min_start = max(today - dt.timedelta(days=RECENT_DAYS_MIN), minute_floor)
    _do_minute_since(symbol, min_start, today)

    tick_start = today - dt.timedelta(days=RECENT_DAYS_TICKS)
    _do_ticks_range(symbol, tick_start, today)

def _moderate(symbol: str) -> None:
    today = _today()
    daily_floor, minute_floor, _ = _symbol_floors(symbol)
    _do_daily_range(symbol, daily_floor, today)
    _do_minute_since(symbol, minute_floor, today)

def _full(symbol: str) -> None:
    today = _today()
    daily_floor, minute_floor, _ = _symbol_floors(symbol)
    _do_daily_range(symbol, daily_floor, today)
    _do_minute_since(symbol, minute_floor, today)
    _do_ticks_range(symbol, daily_floor, today)

# ---------- Public controller -------------------------------------------------

def run_polygon_backfill(symbol: Optional[str], mode: str) -> None:
    """
    Modes:
      recent   -> daily 5d, minute 3d, ticks 1d
      moderate -> daily since incept, minute since 2021-01-01, no ticks
      full     -> all daily, all minute, all ticks
    Behavior:
      - symbol None/''/'ALL' -> run mode for ALL symbols.
      - mode 'all'           -> alias of 'full'.
    """
    m = (mode or "recent").strip().lower()
    if m == "all":
        m = "full"
    if m not in ("recent", "moderate", "full"):
        LOG.warning("Unknown mode '%s' -> using 'recent'", m)
        m = "recent"

    sym = (symbol or "").strip().upper()
    run_all = (sym in ("", "ALL"))

    symbols: List[str] = [sym] if not run_all else _fetch_all_symbols()
    if not symbols:
        LOG.error("[RUN] No symbols to process.")
        return

    LOG.info("[RUN] %s mode for %s", m, "ALL" if run_all else sym)
    ok = 0
    fail = 0
    for s in symbols:
        try:
            if m == "recent":
                _recent(s)
            elif m == "moderate":
                _moderate(s)
            else:
                _full(s)
            ok += 1
        except Exception as e:
            fail += 1
            LOG.error("[RUN] %s %s failed: %s", m, s, e, exc_info=False)

    LOG.info("[RUN] ✅ Completed mode=%s: success=%d, failed=%d", m, ok, fail)
