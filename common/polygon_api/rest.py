# =============================================================================
# common/polygon_api/rest.py
# Version: 2025.10.22-hardened2-floor
#
# CHANGELOG
# - 2025-10-22: hardened2-floor
#   • Respect per-symbol floor for BOTH daily & minute bars:
#       floor(symbol) = max(HARD_MIN_START, list_date(symbol) or None)
#   • Default floor provider auto-queries symbol_metadata.list_date (cached) if DB is available.
#   • Optional override via set_symbol_floor_provider(func).
#   • Retains hardened HTTP: retries, 429 Retry-After, JSON/parse guards.
# =============================================================================

from __future__ import annotations

import os
import time
import logging
import datetime as dt
from functools import lru_cache
from typing import Callable, Dict, Optional

import pandas as pd
import requests
from requests import exceptions as rqexc

LOG = logging.getLogger("polygon_api.rest")
if not LOG.handlers:
    LOG.addHandler(logging.StreamHandler())
LOG.setLevel(logging.INFO)

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "").strip()
BASE_URL_V2 = os.getenv("POLYGON_BASE_URL_V2", "https://api.polygon.io/v2")

# Conservative global floor for equities data
HARD_MIN_START = dt.date(2004, 1, 1)

# -----------------------------------------------------------------------------
# Optional DB support for list_date lookup (symbol_metadata)
# -----------------------------------------------------------------------------
try:
    from common.dbLayer.dbutils import connection  # optional
    _HAVE_DB = True
except Exception:
    connection = None  # type: ignore
    _HAVE_DB = False

@lru_cache(maxsize=10000)
def _lookup_list_date(symbol: str) -> Optional[dt.date]:
    """Best-effort DB lookup of list_date from symbol_metadata; cached."""
    if not _HAVE_DB:
        return None
    try:
        import psycopg
        from psycopg.rows import tuple_row
        with connection() as conn, conn.cursor(row_factory=tuple_row) as cur:  # type: ignore
            cur.execute("SELECT list_date FROM symbol_metadata WHERE symbol = %s", (symbol,))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        # Don't spam logs repeatedly; cache the None result
        LOG.info("list_date lookup failed for %s (%s); using HARD_MIN_START", symbol, e)
        return None

# Pluggable symbol floor provider (default uses DB lookup above)
_SymbolFloorProvider = Callable[[str], Optional[dt.date]]
_symbol_floor_provider: Optional[_SymbolFloorProvider] = None

def set_symbol_floor_provider(provider: _SymbolFloorProvider) -> None:
    """
    Inject a custom floor provider. Signature: provider(symbol) -> date|None
    If provider returns None, HARD_MIN_START will be used.
    """
    global _symbol_floor_provider
    _symbol_floor_provider = provider

def _symbol_floor(symbol: str) -> dt.date:
    """Return per-symbol floor = max(HARD_MIN_START, list_date or provider date or None)."""
    d: Optional[dt.date] = None
    if _symbol_floor_provider is not None:
        try:
            d = _symbol_floor_provider(symbol)
        except Exception as e:
            LOG.warning("symbol floor provider failed for %s: %s", symbol, e)
            d = None
    if d is None:
        d = _lookup_list_date(symbol)
    if d is None:
        return HARD_MIN_START
    return max(HARD_MIN_START, d)

# -----------------------------------------------------------------------------
# Exceptions & HTTP helpers
# -----------------------------------------------------------------------------

class PolygonFatalError(Exception):
    """Non-retryable error (auth/permission/config)."""

class PolygonTransientError(Exception):
    """Retryable error exhausted (network/5xx/timeout/JSON parse)."""

def _get(url: str, params: Optional[Dict] = None) -> requests.Response:
    """Single GET with basic validation and timeout; does NOT retry."""
    if not POLYGON_API_KEY:
        raise PolygonFatalError("POLYGON_API_KEY is not set")
    params = dict(params or {})
    params.setdefault("apiKey", POLYGON_API_KEY)
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r

def _backoff_sleep(attempt: int, base: float = 0.5, cap: float = 8.0) -> None:
    time.sleep(min(cap, base * (2 ** (attempt - 1))))

def _request_json_with_retries(
    url: str,
    params: dict,
    *,
    max_attempts: int = 5,
) -> dict:
    """
    GET + JSON with resilient behavior:
      - 429: honor Retry-After; else exponential backoff; retry.
      - 408/timeouts/connection/5xx: exponential backoff; retry.
      - 400/404: benign "no data" -> return {"results": []}.
      - 401/403: fatal -> raise PolygonFatalError.
      - JSON decode errors: retry; then PolygonTransientError.
    """
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = _get(url, params=params)
            try:
                return resp.json()
            except ValueError as e:
                last_exc = e
                LOG.warning("JSON decode failed (attempt %d/%d): %s", attempt, max_attempts, e)
                if attempt < max_attempts:
                    _backoff_sleep(attempt)
                    continue
                raise PolygonTransientError(f"JSON decode failed after {max_attempts} attempts") from e

        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            # Rate limit
            if status == 429:
                ra = e.response.headers.get("Retry-After")
                try:
                    wait = float(ra) if ra is not None else None
                except Exception:
                    wait = None
                if wait is None:
                    wait = min(8.0, 0.5 * (2 ** (attempt - 1)))
                LOG.warning("Polygon 429 rate limit; sleeping %.2fs (attempt %d/%d).", wait, attempt, max_attempts)
                time.sleep(wait)
                continue
            # Timeout (HTTP 408) → retry
            if status == 408:
                LOG.warning("Polygon 408 timeout (attempt %d/%d). Retrying...", attempt, max_attempts)
                _backoff_sleep(attempt)
                continue
            # 5xx → retry
            if status and 500 <= status < 600:
                LOG.warning("Polygon %d server error (attempt %d/%d). Retrying...", status, attempt, max_attempts)
                _backoff_sleep(attempt)
                continue
            # Bad range / no data
            if status in (400, 404):
                LOG.info("Polygon %d for %s — treating as empty result.", status, url)
                return {"results": []}
            # Auth/config → fatal
            if status in (401, 403):
                raise PolygonFatalError(f"Polygon auth/permission error {status}") from e
            # Other 4xx → fatal
            raise PolygonFatalError(f"Polygon HTTP {status or '4xx'}") from e

        except (rqexc.Timeout, rqexc.ConnectTimeout, rqexc.ReadTimeout) as e:
            last_exc = e
            LOG.warning("Network timeout (attempt %d/%d): %s", attempt, max_attempts, e)
            if attempt < max_attempts:
                _backoff_sleep(attempt)
                continue
            raise PolygonTransientError("Network timeouts exhausted") from e

        except (rqexc.ConnectionError, rqexc.ChunkedEncodingError) as e:
            last_exc = e
            LOG.warning("Network error (attempt %d/%d): %s", attempt, max_attempts, e)
            if attempt < max_attempts:
                _backoff_sleep(attempt)
                continue
            raise PolygonTransientError("Network errors exhausted") from e

        except Exception as e:
            last_exc = e
            LOG.warning("Unexpected error (attempt %d/%d): %s", attempt, max_attempts, e)
            if attempt < max_attempts:
                _backoff_sleep(attempt)
                continue
            raise PolygonTransientError("Unexpected errors exhausted") from e

    raise PolygonTransientError("Retries exhausted") from last_exc

# -----------------------------------------------------------------------------
# Client
# -----------------------------------------------------------------------------

class PolygonRestClient:
    def __init__(self, base_url_v2: str = BASE_URL_V2):
        self.base_v2 = base_url_v2.rstrip("/")

    # ---- Aggregates: 1D bars
    def daily_bars(self, symbol: str, start: dt.date, end: dt.date) -> pd.DataFrame:
        print("Fetching daily bars for", symbol, start, "to", end)
        # Normalize & clamp to per-symbol floor
        if isinstance(start, dt.datetime): start = start.date()
        if isinstance(end, dt.datetime):   end = end.date()
        floor = _symbol_floor(symbol)
        if start < floor: start = floor
        if start > end:
            return _empty_daily()

        url = f"{self.base_v2}/aggs/ticker/{symbol}/range/1/day/{start:%Y-%m-%d}/{end:%Y-%m-%d}"
        params = {"adjusted": "true", "sort": "asc", "limit": 50000}

        try:
            payload = _request_json_with_retries(url, params)
        except PolygonFatalError as e:
            LOG.error("Daily bars fatal for %s (%s→%s): %s", symbol, start, end, e)
            raise
        except PolygonTransientError as e:
            LOG.error("Daily bars transient-exhausted for %s (%s→%s): %s", symbol, start, end, e)
            return _empty_daily()

        results = payload.get("results") or []
        print("Received", len(results), "daily bars")
        if not results:
            return _empty_daily()

        try:
            t = pd.to_datetime([r.get("t", 0) for r in results], unit="ms", utc=True)
            df = pd.DataFrame({
                "t": t,
                "o": [float(r.get("o", 0.0)) for r in results],
                "h": [float(r.get("h", 0.0)) for r in results],
                "l": [float(r.get("l", 0.0)) for r in results],
                "c": [float(r.get("c", 0.0)) for r in results],
                "v": [int(r.get("v", 0) or 0) for r in results],
            })
            return df
        except Exception as e:
            LOG.error("Daily parse error for %s: %s", symbol, e)
            return _empty_daily()

    # ---- Aggregates: 1-minute bars for a single calendar day
    def minute_bars_day(self, symbol: str, day: dt.date) -> pd.DataFrame:
        print("Fetching minute bars for", symbol, day)
        # Normalize & clamp to per-symbol floor (respect list_date)
        if isinstance(day, dt.datetime):
            day = day.date()
        floor = _symbol_floor(symbol)
        if day < floor:
            # Do NOT call HTTP for pre-list_date days
            LOG.info("Minute bars %s %s < floor %s — returning empty.", symbol, day, floor)
            return _empty_minute()

        url = f"{self.base_v2}/aggs/ticker/{symbol}/range/1/minute/{day:%Y-%m-%d}/{day:%Y-%m-%d}"
        params = {"adjusted": "true", "sort": "asc", "limit": 50000}

        try:
            payload = _request_json_with_retries(url, params)
        except PolygonFatalError as e:
            LOG.error("Minute bars fatal for %s %s: %s", symbol, day, e)
            raise
        except PolygonTransientError as e:
            LOG.error("Minute bars transient-exhausted for %s %s: %s", symbol, day, e)
            return _empty_minute()

        results = payload.get("results") or []
        print("Received", len(results), "minute bars")
        if not results:
            return _empty_minute()

        try:
            t = pd.to_datetime([r.get("t", 0) for r in results], unit="ms", utc=True)
            df = pd.DataFrame({
                "t": t,
                "o": [float(r.get("o", 0.0)) for r in results],
                "h": [float(r.get("h", 0.0)) for r in results],
                "l": [float(r.get("l", 0.0)) for r in results],
                "c": [float(r.get("c", 0.0)) for r in results],
                "v": [int(r.get("v", 0) or 0) for r in results],
            })
            return df
        except Exception as e:
            LOG.error("Minute parse error for %s %s: %s", symbol, day, e)
            return _empty_minute()

# -----------------------------------------------------------------------------
# Facade & singleton
# -----------------------------------------------------------------------------

_client_singleton: Optional[PolygonRestClient] = None

def _client() -> PolygonRestClient:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = PolygonRestClient()
    return _client_singleton

def fetch_daily_bars(symbol: str, start: dt.date, end: dt.date) -> pd.DataFrame:
    return _client().daily_bars(symbol, start, end)

def fetch_minute_bars_day(symbol: str, day: dt.date) -> pd.DataFrame:
    return _client().minute_bars_day(symbol, day)

# -----------------------------------------------------------------------------
# Empty frames
# -----------------------------------------------------------------------------

def _empty_daily() -> pd.DataFrame:
    return pd.DataFrame({
        "t": pd.to_datetime([], utc=True),
        "o": [], "h": [], "l": [], "c": [], "v": []
    })

def _empty_minute() -> pd.DataFrame:
    return pd.DataFrame({
        "t": pd.to_datetime([], utc=True),
        "o": [], "h": [], "l": [], "c": [], "v": []
    })
