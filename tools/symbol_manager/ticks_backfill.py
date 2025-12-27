# dbmanager/ticks_backfill.py
# Reflex tick backfill for Polygon v3 trades
# - Writes to public.tick_data (canonical schema)
# - Idempotent: ON CONFLICT (symbol,"timestamp",trade_id) DO NOTHING
# - Paginates Polygon v3 with next_url and ALWAYS appends apiKey on follow-ups
# - Symbols from REFLEX__SYMBOLS_SQL or REFLEX__SYMBOLS (CSV)
# - Dates from REFLEX__START_DATE / REFLEX__END_DATE (inclusive start, exclusive end)

from __future__ import annotations

import os
import sys
import time
import logging as log
import datetime as dt
from typing import Iterable, Iterator, List, Optional, Tuple
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import requests
import psycopg

# ---------------------------
# Config / Environment
# ---------------------------

def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if (v is not None and str(v).strip() != "") else default

def _bool_env(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    v = v.strip().lower()
    if v in ("1", "true", "t", "yes", "y", "on"):
        return True
    if v in ("0", "false", "f", "no", "n", "off"):
        return False
    return default

def _get_dsn() -> str:
    dsn = (
        _env("PG_DSN")
        or _env("REFLEX__PG_DSN")
        or _env("REFLEX__STORAGE__PG_DSN")
        or _env("DATABASE_URL")
    )
    if dsn:
        return dsn
    host = _env("PGHOST", "127.0.0.1")
    port = _env("PGPORT", "5432")
    user = _env("PGUSER", "postgres")
    pwd  = _env("PGPASSWORD")
    db   = _env("PGDATABASE", "postgres")
    if pwd:
        return f"postgresql://{user}:{pwd}@{host}:{port}/{db}"
    else:
        return f"postgresql://{user}@{host}:{port}/{db}"

POLYGON_API_KEY = _env("POLYGON_API_KEY") or _env("POLYGON_KEY") or _env("POLYGON_TOKEN")
START_DATE  = _env("REFLEX__START_DATE")
END_DATE    = _env("REFLEX__END_DATE")
SLEEP_SECS  = float(_env("REFLEX__SLEEP_SECONDS", "0.05"))
LIMIT       = int(_env("REFLEX__LIMIT", "50000"))
SYMBOLS_SQL = _env("REFLEX__SYMBOLS_SQL")
SYMBOLS_CSV = _env("REFLEX__SYMBOLS")
TABLE       = _env("REFLEX__TABLE", "tick_data")
INCLUDE_OTC = _bool_env("REFLEX__INCLUDE_OTC", False)
LOG_LEVEL   = (_env("REFLEX__LOG_LEVEL", "INFO") or "INFO").upper()

log.basicConfig(
    level=getattr(log, LOG_LEVEL, log.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ---------------------------
# SQL (canonical)
# ---------------------------

INSERT_SQL = f"""
    INSERT INTO public.{TABLE}
        (symbol, "timestamp",
         sip_timestamp, participant_timestamp, trf_timestamp,
         price, size, exchange, conditions, tape, trade_id)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ON CONFLICT (symbol, "timestamp", trade_id) DO NOTHING
"""

# ---------------------------
# Helpers
# ---------------------------

def _parse_date(s: str) -> dt.date:
    return dt.datetime.strptime(s, "%Y-%m-%d").date()

def _date_range(start: dt.date, end_exclusive: dt.date) -> Iterator[dt.date]:
    d = start
    while d < end_exclusive:
        yield d
        d += dt.timedelta(days=1)

def _resolve_window() -> Tuple[dt.date, dt.date]:
    if not START_DATE or not END_DATE:
        log.error("REFLEX__START_DATE and REFLEX__END_DATE (YYYY-MM-DD) are required")
        sys.exit(2)
    s = _parse_date(START_DATE)
    e = _parse_date(END_DATE)
    if e <= s:
        log.error("END_DATE must be > START_DATE (exclusive end). Got %s .. %s", s, e)
        sys.exit(2)
    return s, e

def _resolve_symbols(conn: psycopg.Connection) -> List[str]:
    if SYMBOLS_SQL:
        sql = SYMBOLS_SQL.strip()
        if sql.upper().startswith("FROM "):
            sql = "SELECT symbol " + sql
        if not sql.upper().startswith("SELECT "):
            log.error("REFLEX__SYMBOLS_SQL must be a SELECT or a FROM-clause. Got: %s", SYMBOLS_SQL)
            sys.exit(2)
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        return [str(r[0]).strip() for r in rows if r and r[0]]

    if SYMBOLS_CSV:
        return [s.strip() for s in SYMBOLS_CSV.split(",") if s.strip()]

    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM public.symbol_feed ORDER BY symbol")
        rows = cur.fetchall()
    return [str(r[0]).strip() for r in rows if r and r[0]]

def _maybe_filter_otc(symbols: List[str]) -> List[str]:
    return symbols if INCLUDE_OTC else symbols  # no-op: user controls via SQL

# ---------------------------
# Polygon v3 client
# ---------------------------

BASE = "https://api.polygon.io"

def _append_api_key_to_url(url: str, api_key: str) -> str:
    """
    Ensure apiKey param is present (Polygon next_url often omits it).
    """
    u = urlparse(url)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    if "apiKey" not in q:
        q["apiKey"] = api_key
        u = u._replace(query=urlencode(q))
        return urlunparse(u)
    return url

def _fetch_trades(symbol: str, on_date: dt.date, limit: int, api_key: str) -> Iterator[dict]:
    """
    Yield trade objects for a symbol/date from Polygon v3, following next_url,
    ALWAYS appending apiKey on follow-ups to avoid 401.
    """
    url = f"{BASE}/v3/trades/{symbol}"
    params = {"date": on_date.isoformat(), "limit": str(limit), "order": "asc", "sort": "timestamp", "apiKey": api_key}

    session = requests.Session()
    session.headers.update({"User-Agent": "ReflexTicks/2.2 (+https://example)"})

    next_url: Optional[str] = None
    backoff = 0.25
    max_backoff = 8.0
    retries = 0
    max_retries = 8

    while True:
        try:
            if next_url:
                url2 = _append_api_key_to_url(next_url, api_key)
                resp = session.get(url2, timeout=30)
            else:
                resp = session.get(url, params=params, timeout=30)

            if resp.status_code in (429, 500, 502, 503, 504):
                log.warning("[NET] %s (%s) - backing off %.2fs", resp.status_code, resp.reason, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 1.7, max_backoff)
                retries += 1
                if retries > max_retries:
                    log.warning("[NET] giving up after %d retries", retries)
                    break
                continue

            if resp.status_code == 401:
                # Try once to re-append the key and retry immediately if we somehow missed it
                if next_url:
                    url2 = _append_api_key_to_url(next_url, api_key)
                    if url2 != next_url:
                        log.warning("[NET] 401 on next_url; re-appending apiKey and retrying once")
                        next_url = url2
                        continue
                resp.raise_for_status()

            resp.raise_for_status()
            backoff, retries = 0.25, 0
            data = resp.json()

        except requests.RequestException as e:
            log.warning("[NET] %s - backing off %.2fs", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 1.7, max_backoff)
            retries += 1
            if retries > max_retries:
                log.warning("[NET] giving up after %d retries", retries)
                break
            continue

        results = data.get("results") or []
        for tr in results:
            yield tr

        next_url = data.get("next_url")
        if not next_url:
            break

def _normalize_trade(symbol: str, tr: dict) -> Optional[Tuple]:
    """
    Convert a Polygon trade into a tuple for INSERT_SQL.
    """
    ns = tr.get("sip_timestamp") or tr.get("participant_timestamp") or tr.get("trf_timestamp")
    if not ns:
        return None
    ts = dt.datetime.fromtimestamp(ns / 1e9, tz=dt.timezone.utc)

    price = tr.get("price")
    size  = tr.get("size")
    if price is None or size is None:
        return None

    sip_ns  = tr.get("sip_timestamp")
    part_ns = tr.get("participant_timestamp")
    trf_ns  = tr.get("trf_timestamp")

    exchange = tr.get("exchange")
    tape     = tr.get("tape")

    conds = tr.get("conditions") or []
    try:
        conds = [int(c) for c in conds]
    except Exception:
        conds = None  # store NULL if malformed

    trade_id = tr.get("id")
    trade_id = str(trade_id) if trade_id is not None else None

    return (
        symbol,
        ts,                         # "timestamp"
        sip_ns,
        part_ns,
        trf_ns,
        float(price),
        int(size),
        int(exchange) if exchange is not None else None,
        conds,                      # integer[]
        int(tape) if tape is not None else None,
        trade_id
    )

def _insert_rows(conn: psycopg.Connection, rows: List[Tuple]) -> None:
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(INSERT_SQL, rows)

# ---------------------------
# Main
# ---------------------------

def run() -> None:
    if not POLYGON_API_KEY:
        log.error("POLYGON_API_KEY is required")
        sys.exit(2)

    start, end_excl = _resolve_window()

    dsn = _get_dsn()
    with psycopg.connect(dsn, autocommit=True) as conn:
        symbols = _resolve_symbols(conn)
        symbols = _maybe_filter_otc(symbols)

        if not symbols:
            log.error("0 symbols resolved. Check REFLEX__SYMBOLS_SQL or REFLEX__SYMBOLS.")
            sys.exit(2)

        log.info("[env] DSN present=%s  POLYGON_API_KEY present=%s", bool(dsn), bool(POLYGON_API_KEY))
        log.info("[plan] %s -> %s  order=days-first  mode=single  sleep=%s  limit=%s",
                 start.isoformat(), (end_excl - dt.timedelta(days=1)).isoformat(), SLEEP_SECS, LIMIT)
        log.info("[plan] table=%s  symbols_sql=%s", TABLE, SYMBOLS_SQL or "(CSV or default)")
        log.info("[INFO] %d symbols resolved: first=%s last=%s", len(symbols), symbols[0], symbols[-1])

        day_list = list(_date_range(start, end_excl))
        log.info("[PLAN] days-first %s -> %s  (%d days), %d symbols",
                 day_list[0].isoformat(), day_list[-1].isoformat(), len(day_list), len(symbols))

        for di, day in enumerate(day_list, 1):
            log.info("[DAY] %s  (%d/%d)", day.isoformat(), di, len(day_list))
            for si, sym in enumerate(symbols, 1):
                rows: List[Tuple] = []
                try:
                    for tr in _fetch_trades(sym, day, LIMIT, POLYGON_API_KEY):
                        tup = _normalize_trade(sym, tr)
                        if tup:
                            rows.append(tup)
                    _insert_rows(conn, rows)
                    log.info("[T] %s %s rows=%d (sym %d/%d)", day.isoformat(), sym, len(rows), si, len(symbols))
                except KeyboardInterrupt:
                    log.warning("Interrupted by user. Exiting cleanly.")
                    return
                except Exception as e:
                    log.warning("[WARN] %s %s: %s", day.isoformat(), sym, e)
                finally:
                    time.sleep(SLEEP_SECS)

        log.info("[DONE] Backfill complete.")

def main() -> None:
    run()

if __name__ == "__main__":
    main()
