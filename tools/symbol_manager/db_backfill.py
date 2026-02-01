#!/usr/bin/env python3
"""
Reflex2 — Backfill utility (full r... trimmed docstring in this paste)

This file is a patched version of your db_backfill with:
- Smart-by-default "fill holes" planning (skips days already present)
- Old behavior restored via --force
- Trading-day planning (uses trading_calendar if present, else weekdays)
- Compact CSV run log: <repo_root>/logs/backfill/backfill_runs.csv

NOTE: This file is intended to be dropped in as tools/symbol_manager/db_backfill.py
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import psycopg
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# --------------------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------------------


@dataclass
class BackfillStats:
    """Lightweight counters returned by per-symbol runners."""

    planned_days: int = 0
    fetch_days: int = 0
    skipped_days: int = 0
    failed_days: int = 0

    # Row counters (best-effort)
    rows_fetched: int = 0
    rows_inserted: int = 0
    rows_skipped: int = 0
    rows_bad: int = 0

    # Legacy compatibility
    rows_upserted: int = 0

    elapsed_sec: float = 0.0


# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------

LOG = logging.getLogger("backfill")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:backfill:%(message)s",
    datefmt="%H:%M:%S",
)


def _setup_file_logging(repo_root: Path, kind: str, run_id: str) -> None:
    """Add a FileHandler so backfill logs persist to disk (in addition to console)."""
    try:
        log_dir = repo_root / "logs" / "backfill"
        _ensure_dir(log_dir)
        log_path = log_dir / f"{kind}_backfill_{run_id}.log"
        for h in list(LOG.handlers):
            if getattr(h, "baseFilename", None) and str(getattr(h, "baseFilename")) == str(log_path):
                return
        fh = logging.FileHandler(str(log_path), encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s:backfill:%(message)s", datefmt="%H:%M:%S"))
        LOG.addHandler(fh)
        LOG.info("backfill.logfile %s", log_path)
    except Exception as e:
        try:
            LOG.warning("backfill.logfile.setup_failed err=%r", e)
        except Exception:
            pass


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _repo_root_from_here() -> Path:
    """
    Resolve repo root from this file location: tools/symbol_manager/db_backfill.py -> repo root.
    """
    here = Path(__file__).resolve()
    # .../tools/symbol_manager/db_backfill.py
    return here.parents[2]


def _parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _date_range_inclusive(start: dt.date, end: dt.date) -> list[dt.date]:
    if end < start:
        return []
    out: list[dt.date] = []
    d = start
    while d <= end:
        out.append(d)
        d += dt.timedelta(days=1)
    return out


def _chunks_contiguous(days: list[dt.date]) -> list[tuple[dt.date, dt.date]]:
    """
    Turn sorted list of dates into contiguous (start,end) ranges.
    """
    if not days:
        return []
    days = sorted(days)
    ranges: list[tuple[dt.date, dt.date]] = []
    s = days[0]
    prev = days[0]
    for d in days[1:]:
        if d == prev + dt.timedelta(days=1):
            prev = d
            continue
        ranges.append((s, prev))
        s = d
        prev = d
    ranges.append((s, prev))
    return ranges


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        if x is None:
            return default
        return int(x)
    except Exception:
        return default


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rand_run_id() -> str:
    return "".join(random.choice("0123456789abcdef") for _ in range(8))


# --- Tick day boundaries and timestamp normalization ---
NY_TZ = ZoneInfo("America/New_York")

def market_day_bounds_utc(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """Return UTC-aware [start,end) bounds for the NY market date (midnight-to-midnight America/New_York)."""
    start_local = dt.datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=NY_TZ)
    end_local = start_local + dt.timedelta(days=1)
    return start_local.astimezone(dt.timezone.utc), end_local.astimezone(dt.timezone.utc)

def _epoch_to_utc(ts: Any) -> Optional[dt.datetime]:
    """Normalize epoch seconds/ms/us/ns -> UTC datetime."""
    if ts is None:
        return None
    try:
        v = int(ts)
    except Exception:
        return None

    # Magnitude-based unit detection
    if v > 10**17:      # ns
        sec = v / 1e9
    elif v > 10**14:    # us
        sec = v / 1e6
    elif v > 10**11:    # ms
        sec = v / 1e3
    else:               # seconds
        sec = float(v)

    try:
        return dt.datetime.fromtimestamp(sec, tz=dt.timezone.utc)
    except Exception:
        return None

def extract_trade_ts_utc(tr: dict[str, Any]) -> Optional[dt.datetime]:
    """Extract a UTC timestamp from a Polygon trade dict across common fields."""
    for k in ("sip_timestamp", "participant_timestamp", "trf_timestamp", "timestamp", "t"):
        d = _epoch_to_utc(tr.get(k))
        if d is not None:
            return d
    return None


def apply_start_symbol(symbols: list[str], start_symbol: str | None, start_after: str | None) -> list[str]:
    """Return a filtered, deterministic symbol list based on resume pointers.

    - start_symbol: inclusive resume point (>= start_symbol)
    - start_after: exclusive resume point (> start_after)

    The returned list is unique, uppercased, and sorted.
    """
    if not symbols:
        return symbols

    # Normalize + sort for deterministic resume behavior
    syms = sorted({s.strip().upper() for s in symbols if s and s.strip()})

    if start_symbol and start_after:
        raise ValueError("Use only one of --start-symbol or --start-after (or --start-at).")

    needle = start_symbol or start_after
    if not needle:
        return syms

    needle = needle.strip().upper()

    if start_after:
        for i, s in enumerate(syms):
            if s > needle:
                return syms[i:]
        return []
    else:
        for i, s in enumerate(syms):
            if s >= needle:
                return syms[i:]
        return []


# --------------------------------------------------------------------------------------
# Tick-archived policy (KISS)
# --------------------------------------------------------------------------------------

TICK_ARCHIVE_CUTOFF = dt.date(2026, 1, 1)

# Tick-archived policy (KISS)
TICK_ARCHIVE_CUTOFF = dt.date(2026, 1, 1)

def _filters_has_tick_archived(filters: Any) -> bool:
    """
    symbol_metadata.filters can be:
      - text (e.g., 'tick_archived')
      - text[] (Python list of strings)
      - NULL
    We treat any occurrence of 'tick_archived' (case-insensitive) as true.
    """
    if filters is None:
        return False

    # If it's a list/tuple/set, check each element
    if isinstance(filters, (list, tuple, set)):
        for x in filters:
            if x is None:
                continue
            if str(x).strip().lower() == "tick_archived":
                return True
        return False

    # Otherwise treat as scalar string-ish
    try:
        return str(filters).strip().lower() == "tick_archived"
    except Exception:
        return False


def _clamp_eff_start_for_tick_archive(kind: str, eff_start: dt.date, filters: Any) -> dt.date:
    """
    If symbol_metadata.filters indicates tick archival, ignore tick planning/fetching earlier than 2026-01-01.
    Applies ONLY to kind='tick'.
    """
    if kind != "tick":
        return eff_start
    if _filters_has_tick_archived(filters) and eff_start < TICK_ARCHIVE_CUTOFF:
        return TICK_ARCHIVE_CUTOFF
    return eff_start



# --------------------------------------------------------------------------------------
# Env loading (keep your existing behavior)
# --------------------------------------------------------------------------------------


def load_env(dotenv_path: Path) -> None:
    LOG.info("db_backfill.loaded file=%s tick_support=%s", __file__, hasattr(PolygonClient, "iter_trades_for_day"))
    """
    Minimal .env loader: KEY=VALUE, ignores comments and section headers.
    Keeps current behavior: doesn't override existing env vars.
    """
    if not dotenv_path.exists():
        LOG.warning('No .env found at "%s"', str(dotenv_path))
        return
    LOG.info('Loading .env from "%s" ...', str(dotenv_path))
    for raw in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if line.startswith(";"):
            continue
        # ignore section headers like "# ---------- core ----------"
        if line.startswith("[") and line.endswith("]"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and (k not in os.environ):
            os.environ[k] = v


def get_env(key: str, default: str | None = None) -> str | None:
    v = os.environ.get(key)
    return v if v is not None else default


# --------------------------------------------------------------------------------------
# Polygon client (keep existing patterns)
# --------------------------------------------------------------------------------------


class PolygonClient:
    def __init__(self, api_key: str, base_url: str = "https://api.polygon.io"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

        # Network hardening: automatic retries with exponential backoff for transient failures.
        retries = Retry(
            total=8,
            connect=8,
            read=8,
            status=8,
            backoff_factor=1.0,  # 1s, 2s, 4s, 8s...
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries, pool_connections=20, pool_maxsize=20)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _get(self, path_or_url: str, params: Optional[dict[str, Any]]) -> dict[str, Any]:
        """HTTP GET helper.

        Accepts either a relative API path (e.g. '/v3/trades/SPY') or a fully-qualified URL
        (e.g. 'https://api.polygon.io/v3/trades/SPY?...').

        If params is provided, apiKey is injected via query parameters.
        If params is None, the URL is requested as-is (used for Polygon next_url pagination
        where the query string is already present).
        """
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            url = path_or_url
        else:
            url = f"{self.base_url}{path_or_url}"

        if params is not None:
            q = dict(params)
            q["apiKey"] = self.api_key
            resp = self.session.get(url, params=q, timeout=(10, 120))
        else:
            resp = self.session.get(url, timeout=(10, 120))

        resp.raise_for_status()
        return resp.json()

    def fetch_daily_bars(self, symbol: str, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
        path = f"/v2/aggs/ticker/{symbol}/range/1/day/{start.isoformat()}/{end.isoformat()}"
        js = self._get(path, {"adjusted": "true", "sort": "asc", "limit": 50000})
        return js.get("results", []) or []

    def fetch_minute_bars(self, symbol: str, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
        path = f"/v2/aggs/ticker/{symbol}/range/1/minute/{start.isoformat()}/{end.isoformat()}"
        js = self._get(path, {"adjusted": "true", "sort": "asc", "limit": 50000})
        return js.get("results", []) or []

    # ---------------------------------------------------------------------
    # Ticks (Polygon v3 trades) — day-scoped, follows next_url safely
    # ---------------------------------------------------------------------

    def _append_api_key_to_url(self, url: str) -> str:
        """Ensure apiKey is present on a Polygon next_url (Polygon sometimes omits it)."""
        try:
            from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
            u = urlparse(url)
            q = dict(parse_qsl(u.query, keep_blank_values=True))
            if "apiKey" not in q:
                q["apiKey"] = self.api_key
                u = u._replace(query=urlencode(q))
                return urlunparse(u)
            return url
        except Exception:
            join = "&" if "?" in url else "?"
            return f"{url}{join}apiKey={self.api_key}"

    def iter_trades_for_day(self, symbol: str, on_date: dt.date, limit: int = 5000):
        """
        Yield Polygon v3 trade dicts for a symbol on a given *market date*.
        """
        sym = (symbol or "").upper().strip()
        if not sym:
            return

        day_start_utc, day_end_utc = market_day_bounds_utc(on_date)
        ts_gte = int(day_start_utc.timestamp() * 1_000_000_000)   # ns
        ts_lt  = int(day_end_utc.timestamp()   * 1_000_000_000)   # ns

        url = f"{self.base_url}/v3/trades/{sym}"
        params = {
            "timestamp.gte": str(ts_gte),
            "timestamp.lt":  str(ts_lt),
            "limit": str(limit),
            "order": "asc",
            "sort": "timestamp",
            "apiKey": self.api_key,
        }

        next_url: Optional[str] = None
        backoff = 0.25

        while True:
            if next_url:
                data = self._get(next_url, None)
            else:
                data = self._get(url, params)

            results = data.get("results") or []
            for tr in results:
                yield tr

            next_url = data.get("next_url")
            if not next_url:
                break
            if "apiKey=" not in next_url:
                joiner = "&" if "?" in next_url else "?"
                next_url = f"{next_url}{joiner}apiKey={self.api_key}"

            time.sleep(backoff)
            backoff = min(1.5, backoff * 1.25)


class Pg:
    def __init__(self, dsn: str):
        self.conn = psycopg.connect(dsn)
        self.conn.autocommit = False

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def get_symbol_list(self, symbol: str) -> list[tuple[str, dt.date | None, str | None]]:
        """
        Returns list of (symbol, list_date, filters) from public.symbol_metadata.
        Existing behavior: if symbol == ALL, get all symbols.
        """
        with self.conn.cursor() as cur:
            if symbol.upper() == "ALL":
                cur.execute("SELECT symbol, list_date, filters FROM public.symbol_metadata ORDER BY symbol")
            else:
                cur.execute(
                    "SELECT symbol, list_date, filters FROM public.symbol_metadata WHERE symbol=%s",
                    (symbol.upper(),),
                )
            return [(r[0], r[1], r[2]) for r in cur.fetchall()]

    def get_trading_days(self, start: dt.date, end: dt.date) -> list[dt.date]:
        """
        Return trading days in [start, end] inclusive.
        Uses public.trading_calendar when available/populated; otherwise falls back to Mon–Fri.
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT session_date
                    FROM public.trading_calendar
                    WHERE is_trading_day = TRUE
                      AND session_date >= %s
                      AND session_date <= %s
                    ORDER BY session_date
                    """,
                    (start, end),
                )
                rows = cur.fetchall()
            if rows:
                return [row[0] for row in rows]
        except Exception:
            pass

        out: list[dt.date] = []
        d = start
        while d <= end:
            if d.weekday() < 5:
                out.append(d)
            d += dt.timedelta(days=1)
        return out

    def get_expected_days(self, kind: str, start: dt.date, end: dt.date) -> list[dt.date]:
        """
        Expected day buckets for backfill planning.
        For daily/minute/tick backfills, plan over trading days (or weekdays fallback).
        """
        return self.get_trading_days(start, end)

    def get_present_days(self, kind: str, symbol: str, start: dt.date, end: dt.date) -> set[dt.date]:
        """
        Return set of day buckets present for (kind,symbol) within [start,end] inclusive.
        Presence definition (simple): at least one row exists for that day.

        NOTE for ticks:
          We bucket by NY "market date" so it matches market_day_bounds_utc()/has_tick_day().
        """
        symbol = symbol.upper()
        if kind == "daily":
            table = "public.daily_bars"
            ts_col = "timestamp"
        elif kind == "minute":
            table = "public.minute_bars"
            ts_col = "timestamp"
        elif kind == "tick":
            table = "public.tick_data"
            ts_col = "timestamp"
        else:
            raise ValueError(f"Unknown kind: {kind}")

        with self.conn.cursor() as cur:
            if kind == "tick":
                start_ts, _ = market_day_bounds_utc(start)
                _, end_ts = market_day_bounds_utc(end + dt.timedelta(days=1))
                cur.execute(
                    f"""
                    SELECT DISTINCT ({ts_col} AT TIME ZONE 'America/New_York')::date AS d
                    FROM {table}
                    WHERE symbol = %s
                      AND {ts_col} >= %s
                      AND {ts_col} <  %s
                    """,
                    (symbol, start_ts, end_ts),
                )
            else:
                cur.execute(
                    f"""
                    SELECT DISTINCT ({ts_col} AT TIME ZONE 'UTC')::date AS d
                    FROM {table}
                    WHERE symbol = %s
                      AND ({ts_col} AT TIME ZONE 'UTC')::date >= %s
                      AND ({ts_col} AT TIME ZONE 'UTC')::date <= %s
                    """,
                    (symbol, start, end),
                )
            return {row[0] for row in cur.fetchall()}

    def has_tick_day(self, symbol: str, day: dt.date) -> bool:
        """Fast precheck: does tick_data contain at least one row for (symbol, NY market day)?"""
        symbol = (symbol or "").upper()
        start_ts, end_ts = market_day_bounds_utc(day)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM public.tick_data
                WHERE symbol = %s
                  AND "timestamp" >= %s
                  AND "timestamp" <  %s
                LIMIT 1
                """,
                (symbol, start_ts, end_ts),
            )
            return cur.fetchone() is not None

    def upsert_daily_rows(self, symbol: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        with self.conn.cursor() as cur:
            for r in rows:
                ts = dt.datetime.fromtimestamp(r["t"] / 1000, tz=dt.timezone.utc)
                cur.execute(
                    """
                    INSERT INTO public.daily_bars(symbol, timestamp, open, high, low, close, volume)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, timestamp) DO UPDATE SET
                      open=EXCLUDED.open,
                      high=EXCLUDED.high,
                      low =EXCLUDED.low,
                      close=EXCLUDED.close,
                      volume=EXCLUDED.volume
                    """,
                    (symbol, ts, r.get("o"), r.get("h"), r.get("l"), r.get("c"), r.get("v")),
                )
        return len(rows)

    def upsert_minute_rows(self, symbol: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        with self.conn.cursor() as cur:
            for r in rows:
                ts = dt.datetime.fromtimestamp(r["t"] / 1000, tz=dt.timezone.utc)
                cur.execute(
                    """
                    INSERT INTO public.minute_bars(symbol, timestamp, open, high, low, close, volume)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, timestamp) DO UPDATE SET
                      open=EXCLUDED.open,
                      high=EXCLUDED.high,
                      low =EXCLUDED.low,
                      close=EXCLUDED.close,
                      volume=EXCLUDED.volume
                    """,
                    (symbol, ts, r.get("o"), r.get("h"), r.get("l"), r.get("c"), r.get("v")),
                )
        return len(rows)

    def upsert_tick_rows(self, symbol: str, rows: list[tuple[Any, ...]]) -> int:
        """Insert tick rows into public.tick_data (canonical schema)."""
        if not rows:
            return 0

        sql = """
            INSERT INTO public.tick_data
                (symbol, "timestamp", sip_timestamp, participant_timestamp, trf_timestamp,
                 price, size, exchange, conditions, tape, trade_id)
            VALUES
                (%s, %s, %s, %s, %s,
                 %s, %s, %s, %s, %s, %s)
            ON CONFLICT (symbol, "timestamp", trade_id) DO NOTHING
        """

        with self.conn.cursor() as cur:
            try:
                cur.executemany(sql, rows)
                ins = cur.rowcount
                if ins is None or ins < 0:
                    ins = len(rows)
                return int(ins)
            except Exception:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                raise

    def upsert_ticks(self, rows: list[tuple[Any, ...]]) -> tuple[int, int, int]:
        """Insert ticks and report (inserted, skipped, bad)."""
        if not rows:
            return (0, 0, 0)
        sym = str(rows[0][0]) if rows and rows[0] else None
        ins = self.upsert_tick_rows(sym or "", rows)
        sk = max(0, len(rows) - ins)
        return (ins, sk, 0)


def _plan_missing_days(
    db: Pg,
    *,
    kind: str,
    symbol: str,
    eff_start: dt.date,
    until: dt.date,
    force: bool,
) -> tuple[list[dt.date], set[dt.date]]:
    expected_days = db.get_expected_days(kind, eff_start, until)
    if force:
        return expected_days, set()
    present = db.get_present_days(kind, symbol, eff_start, until)
    missing = [d for d in expected_days if d not in present]
    return missing, present


def _run_daily_for_symbol(
    *,
    db: Pg,
    poly: PolygonClient,
    symbol: str,
    eff_start: dt.date,
    until: dt.date,
    commit_every: int,
    force: bool,
) -> BackfillStats:
    st = BackfillStats()
    t0 = time.time()
    missing_days, _present = _plan_missing_days(db, kind="daily", symbol=symbol, eff_start=eff_start, until=until, force=force)
    st.planned_days = len(db.get_expected_days("daily", eff_start, until))
    st.fetch_days = len(missing_days)
    st.skipped_days = st.planned_days - st.fetch_days

    ranges = _chunks_contiguous(missing_days)

    for a, b in ranges:
        LOG.info("[%s] DAILY %s → %s (clamped from %s)", symbol, a.isoformat(), b.isoformat(), eff_start.isoformat())
        try:
            rows = poly.fetch_daily_bars(symbol, a, b)
        except Exception as e:
            days = (b - a).days + 1
            st.failed_days += max(days, 1)
            LOG.error("[%s] daily fetch failed for %s → %s: %s", symbol, a.isoformat(), b.isoformat(), e)
            continue

        n = db.upsert_daily_rows(symbol, rows)
        LOG.info("[%s] daily fetched=%d upserted=%d", symbol, len(rows), n)
        st.rows_upserted += n
        if commit_every <= 1:
            db.commit()
    st.elapsed_sec = time.time() - t0
    return st


def _run_minute_for_symbol(
    *,
    db: Pg,
    poly: PolygonClient,
    symbol: str,
    eff_start: dt.date,
    until: dt.date,
    commit_every: int,
    force: bool,
) -> BackfillStats:
    st = BackfillStats()
    t0 = time.time()
    missing_days, _present = _plan_missing_days(db, kind="minute", symbol=symbol, eff_start=eff_start, until=until, force=force)
    st.planned_days = len(db.get_expected_days("minute", eff_start, until))
    st.fetch_days = len(missing_days)
    st.skipped_days = st.planned_days - st.fetch_days

    ranges = _chunks_contiguous(missing_days)

    for a, b in ranges:
        LOG.info("[%s] MINUTE %s → %s (clamped from %s)", symbol, a.isoformat(), b.isoformat(), eff_start.isoformat())
        try:
            rows = poly.fetch_minute_bars(symbol, a, b)
        except Exception as e:
            days = (b - a).days + 1
            st.failed_days += max(days, 1)
            LOG.error("[%s] minute fetch failed for %s → %s: %s", symbol, a.isoformat(), b.isoformat(), e)
            continue

        n = db.upsert_minute_rows(symbol, rows)
        LOG.info("[%s] minute fetched=%d upserted=%d", symbol, len(rows), n)
        st.rows_upserted += n
        if commit_every <= 1:
            db.commit()
    st.elapsed_sec = time.time() - t0
    return st


def _normalize_polygon_trade_to_tick_row(symbol: str, tr: dict[str, Any]) -> Optional[tuple[Any, ...]]:
    """Map a Polygon v3 trade object into the canonical public.tick_data row tuple."""
    ts = extract_trade_ts_utc(tr)
    if ts is None:
        return None

    price = tr.get("price")
    size = tr.get("size")
    if price is None or size is None:
        return None
    try:
        size_i = int(size)
    except Exception:
        return None
    if size_i <= 0:
        return None
    size = size_i

    sip_ns = tr.get("sip_timestamp")
    part_ns = tr.get("participant_timestamp")
    trf_ns = tr.get("trf_timestamp")

    exchange = tr.get("exchange")
    tape = tr.get("tape")

    conds = tr.get("conditions") or []
    try:
        conds = [int(c) for c in conds]
    except Exception:
        conds = None

    trade_id = tr.get("id")
    trade_id = str(trade_id) if trade_id is not None else None
    if not trade_id:
        return None

    return (
        symbol.upper(),
        ts,
        sip_ns,
        part_ns,
        trf_ns,
        float(price),
        int(size),
        int(exchange) if exchange is not None else None,
        conds,
        int(tape) if tape is not None else None,
        trade_id,
    )


def _run_tick_for_symbol(
    *,
    db: Pg,
    poly: PolygonClient,
    symbol: str,
    eff_start: dt.date,
    until: dt.date,
    commit_every: int,
    force: bool,
) -> BackfillStats:
    st = BackfillStats()
    missing_days, _present = _plan_missing_days(db, kind="tick", symbol=symbol, eff_start=eff_start, until=until, force=force)
    st.planned_days = len(db.get_expected_days("tick", eff_start, until))
    st.fetch_days = len(missing_days)
    st.skipped_days = st.planned_days - st.fetch_days

    BATCH = 5000

    for day in missing_days:
        try:
            if not force and db.has_tick_day(symbol, day):
                LOG.info("[%s] TICK %s precheck hit — skipping Polygon fetch", symbol, day.isoformat())
                continue
            LOG.info("[%s] TICK %s", symbol, day.isoformat())
            batch: list[tuple[Any, ...]] = []
            total_day = 0
            in_bounds_day = 0
            inserted_day = 0
            bad_day = 0
            oob_day = 0

            day_start_utc, day_end_utc = market_day_bounds_utc(day)

            for tr in poly.iter_trades_for_day(symbol, day):
                total_day += 1

                ts = extract_trade_ts_utc(tr)
                if ts is None:
                    bad_day += 1
                    continue
                if not (day_start_utc <= ts < day_end_utc):
                    oob_day += 1
                    continue

                row = _normalize_polygon_trade_to_tick_row(symbol, tr)
                if not row:
                    bad_day += 1
                    continue

                batch.append(row)
                in_bounds_day += 1

                if len(batch) >= BATCH:
                    ins, sk, bad = db.upsert_ticks(batch)
                    inserted_day += ins
                    st.rows_inserted += ins
                    st.rows_skipped += sk
                    st.rows_bad += bad
                    batch.clear()

            if batch:
                ins, sk, bad = db.upsert_ticks(batch)
                inserted_day += ins
                st.rows_inserted += ins
                st.rows_skipped += sk
                st.rows_bad += bad
                batch.clear()

            st.rows_fetched += in_bounds_day

            LOG.info(
                "[%s] tick day=%s total=%d in_bounds=%d inserted=%d skipped=%d bad=%d oob=%d",
                symbol,
                day.isoformat(),
                total_day,
                in_bounds_day,
                inserted_day,
                (in_bounds_day - inserted_day),
                bad_day,
                oob_day,
            )

            if commit_every <= 1:
                db.commit()
        except Exception as e:
            st.failed_days += 1
            LOG.warning("[%s] tick failed day=%s err=%r", symbol, day.isoformat(), e)
            try:
                db.rollback()
            except Exception:
                pass

    return st


def _append_run_log(
    repo_root: Path,
    *,
    run_id: str,
    kind: str,
    symbol_mode: str,
    since: dt.date,
    until: dt.date,
    force: bool,
    plan_syms: int,
    planned_days: int,
    fetch_days: int,
    skipped_days: int,
    failed_days: int,
    rows_upserted: int,
    status: str,
    msg: str,
) -> None:
    log_dir = repo_root / "logs" / "backfill"
    _ensure_dir(log_dir)
    log_path = log_dir / "backfill_runs.csv"

    header = [
        "ts_utc",
        "run_id",
        "kind",
        "symbol_mode",
        "since",
        "until",
        "force",
        "plan_syms",
        "plan_days",
        "fetch_days",
        "skipped_days",
        "failed_days",
        "rows_upserted",
        "status",
        "msg",
    ]

    row = [
        _utc_now_iso(),
        run_id,
        kind,
        symbol_mode,
        since.isoformat(),
        until.isoformat(),
        "1" if force else "0",
        str(plan_syms),
        str(planned_days),
        str(fetch_days),
        str(skipped_days),
        str(failed_days),
        str(rows_upserted),
        status,
        msg.replace("\n", " ").strip(),
    ]

    write_header = not log_path.exists()
    with log_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(header)
        w.writerow(row)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Reflex2 DB backfill (daily/minute/tick)")
    p.add_argument("--kind", choices=["daily", "minute", "tick"], required=True)
    p.add_argument("--symbol", default="ALL", help="Symbol or ALL")
    p.add_argument("--since", required=True, help="YYYY-MM-DD")
    p.add_argument("--until", default=None, help="YYYY-MM-DD (inclusive); default=today UTC")
    p.add_argument("--dry-run", action="store_true", help="Plan only; do not fetch/insert")
    p.add_argument("--commit-every", type=int, default=1)
    p.add_argument("--start-symbol", default=None, help="Resume processing at this symbol (inclusive, lexical order).")
    p.add_argument("--start-after", default=None, help="Resume processing after this symbol (exclusive, lexical order).")
    p.add_argument("--start-at", default=None, help="(Deprecated) Alias for --start-symbol.")
    p.add_argument("--force", action="store_true", help="Refetch full range even if data exists (old behavior).")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    args = build_arg_parser().parse_args(argv)

    repo_root = _repo_root_from_here()
    load_env(repo_root / ".env")

    dsn = (
        get_env("REFLEX__PG_DSN")
        or get_env("REFLEX_PG_DSN")
        or get_env("PG_DSN")
        or ""
    )

    if not dsn:
        LOG.error("Missing REFLEX__PG_DSN (or PG_DSN) in environment.")
        return 2

    api_key = get_env("POLYGON_API_KEY") or ""
    if not api_key:
        LOG.error("Missing POLYGON_API_KEY in environment.")
        return 2

    since = _parse_date(args.since)
    until = _parse_date(args.until) if args.until else dt.datetime.now(dt.timezone.utc).date()

    run_id = _rand_run_id()
    _setup_file_logging(repo_root, args.kind, run_id)
    symbol_mode = args.symbol.upper()

    db = Pg(dsn)
    poly = PolygonClient(api_key)

    total_planned_days = 0
    total_fetch_days = 0
    total_skipped_days = 0
    total_failed_days = 0
    total_rows = 0

    status = "OK"
    msg = ""

    try:
        sym_list = db.get_symbol_list(args.symbol)  # (symbol, list_date, filters)

        # Resume pointer support (low-risk: only filters the symbol list)
        start_symbol = (args.start_symbol or args.start_at)
        start_after = args.start_after
        if start_symbol and start_after:
            raise ValueError("Use only one of --start-symbol / --start-after (or --start-at).")
        if start_symbol or start_after:
            wanted = set(apply_start_symbol([s for (s, _ld, _f) in sym_list], start_symbol, start_after))
            sym_list = [(s, ld, f) for (s, ld, f) in sym_list if s in wanted]
            sym_list.sort(key=lambda x: x[0])

        LOG.info("Symbols: %d", len(sym_list))

        for sym, list_date, filters in sym_list:
            eff_start = max(since, list_date) if list_date else since

            # Tick-only archive clamp: ignore any tick planning/fetching < 2026-01-01 when flagged.
            eff_start = _clamp_eff_start_for_tick_archive(args.kind, eff_start, filters)

            if until < eff_start:
                continue

            if args.dry_run:
                missing_days, present = _plan_missing_days(db, kind=args.kind, symbol=sym, eff_start=eff_start, until=until, force=args.force)
                planned = len(db.get_expected_days(args.kind, eff_start, until))
                LOG.info("[%s] plan kind=%s expected=%d present=%d missing=%d", sym, args.kind, planned, len(present), len(missing_days))
                total_planned_days += planned
                total_fetch_days += len(missing_days)
                total_skipped_days += max(0, planned - len(missing_days))
                continue

            if args.kind == "daily":
                st = _run_daily_for_symbol(db=db, poly=poly, symbol=sym, eff_start=eff_start, until=until, commit_every=args.commit_every, force=args.force)
            elif args.kind == "minute":
                st = _run_minute_for_symbol(db=db, poly=poly, symbol=sym, eff_start=eff_start, until=until, commit_every=args.commit_every, force=args.force)
            else:
                st = _run_tick_for_symbol(db=db, poly=poly, symbol=sym, eff_start=eff_start, until=until, commit_every=args.commit_every, force=args.force)

            total_planned_days += st.planned_days
            total_fetch_days += st.fetch_days
            total_skipped_days += st.skipped_days
            total_failed_days += st.failed_days
            total_rows += st.rows_upserted

            if args.commit_every > 1:
                pass

        db.commit()

    except KeyboardInterrupt:
        LOG.warning("Interrupted; committing partial work and exiting…")
        try:
            db.commit()
        except Exception:
            pass
        status = "WARN"
        msg = "Interrupted"
        _append_run_log(
            repo_root,
            run_id=run_id,
            kind=args.kind,
            symbol_mode=symbol_mode,
            since=since,
            until=until,
            force=args.force,
            plan_syms=len(sym_list) if "sym_list" in locals() else 0,
            planned_days=total_planned_days,
            fetch_days=total_fetch_days,
            skipped_days=total_skipped_days,
            failed_days=total_failed_days,
            rows_upserted=total_rows,
            status=status,
            msg=msg,
        )
        return 130

    except Exception as e:
        LOG.exception("Backfill failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
        status = "FAIL"
        msg = f"{type(e).__name__}: {e}"
        _append_run_log(
            repo_root,
            run_id=run_id,
            kind=args.kind,
            symbol_mode=symbol_mode,
            since=since,
            until=until,
            force=args.force,
            plan_syms=len(sym_list) if "sym_list" in locals() else 0,
            planned_days=total_planned_days,
            fetch_days=total_fetch_days,
            skipped_days=total_skipped_days,
            failed_days=total_failed_days,
            rows_upserted=total_rows,
            status=status,
            msg=msg,
        )
        return 1

    finally:
        db.close()

    msg = f"planned={total_planned_days} fetch={total_fetch_days} skip={total_skipped_days} rows={total_rows}"
    _append_run_log(
        repo_root,
        run_id=run_id,
        kind=args.kind,
        symbol_mode=symbol_mode,
        since=since,
        until=until,
        force=args.force,
        plan_syms=len(sym_list) if "sym_list" in locals() else 0,
        planned_days=total_planned_days,
        fetch_days=total_fetch_days,
        skipped_days=total_skipped_days,
        failed_days=total_failed_days,
        rows_upserted=total_rows,
        status=status,
        msg=msg,
    )

    LOG.info("Done. %s", msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
