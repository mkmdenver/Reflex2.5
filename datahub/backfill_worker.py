# datahub/backfill_worker.py
#
# Backfill worker: listens for backfill requests on CHANNELS["backfill"]
# and runs "today-only" backfills for:
#
#   - minute bars (WATCH tier promotion)
#   - a small tail of ticks (WARM tier promotion)
#
# Payload contract (emitted by datahub.tier_listener):
#   {
#     "symbol": "ASNS",
#     "kinds": ["minute"]            # WATCH
#     "scope": "today",
#     "requested_by": "spy_momo",
#     "ts": 1731851234.567
#   }
#
# or
#
#   {
#     "symbol": "ASNS",
#     "kinds": ["minute", "tick"],   # WARM
#     "scope": "today",
#     "requested_by": "spy_momo",
#     "ts": 1731851234.890
#   }

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, date, timezone, timedelta
from typing import Iterable, List, Dict, Any

import psycopg
import requests
from requests import HTTPError

from common.bus import subscribe, unpack, CHANNELS
from common import logging as log

COMPONENT = "datahub.backfill"

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

PG_DSN = (
    os.getenv("REFLEX_PG_DSN")
    or os.getenv("REFLEX__PG_DSN")
    or os.getenv("DATABASE_URL")
)

POLYGON_KEY = (
    os.getenv("POLYGON_API_KEY")
    or os.getenv("POLYGON_KEY")
    or os.getenv("POLY_API_KEY")
)

POLYGON_AGGS_URL = os.getenv(
    "POLYGON_AGGS_URL_BASE",
    "https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/minute/{day}/{day}",
)

# Stocks trades v3 endpoint
POLYGON_TRADES_V3_URL = os.getenv(
    "POLYGON_TRADES_V3_URL_BASE",
    "https://api.polygon.io/v3/trades/{symbol}",
)

# We only need a *tail* of ticks to warm up the model, not the full day.
TICK_TAIL_LIMIT = int(os.getenv("REFLEX_TICK_TAIL_LIMIT", "500"))

# Once we discover tick backfill is really unavailable (e.g. no key),
# we can flip this to False so future calls are cheap no-ops.
TICK_BACKFILL_AVAILABLE = True


# ---------------------------------------------------------------------------
# Trading-day helper
# ---------------------------------------------------------------------------

def _trading_day_utc() -> date:
    """For now just treat 'today' in UTC as the trading date.

    Later you can:
      - drive this off a calendar table, or
      - use an explicit REFLEX_TRADING_DATE env when replaying.
    """
    return datetime.now(tz=timezone.utc).date()


# ---------------------------------------------------------------------------
# Minute backfill (today only)
# ---------------------------------------------------------------------------

def _fetch_polygon_minutes_today(symbol: str) -> List[Dict[str, Any]]:
    if not POLYGON_KEY:
        log.warn(COMPONENT, "polygon_key_missing", symbol=symbol)
        return []

    day = _trading_day_utc().isoformat()
    url = POLYGON_AGGS_URL.format(symbol=symbol, day=day)
    params = {
        "adjusted": "true",
        "sort": "asc",
        "limit": 50000,
        "apiKey": POLYGON_KEY,
    }
    t0 = time.time()
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results") or []
    log.info(
        COMPONENT,
        "polygon_minutes_fetched",
        symbol=symbol,
        day=day,
        n=len(results),
        ms=int(1000 * (time.time() - t0)),
    )
    return results


def _insert_minutes(symbol: str, bars: Iterable[Dict[str, Any]]) -> int:
    if not PG_DSN:
        log.error(COMPONENT, "pg_dsn_missing")
        return 0

    # Polygon aggs payload:
    #   t: start timestamp in ms
    #   o/h/l/c: OHLC
    #   v: volume
    rows: List[Dict[str, Any]] = []
    for b in bars:
        ts_ms = b.get("t")
        if ts_ms is None:
            continue
        # Polygon sends ms since epoch (UTC)
        ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        rows.append(
            {
                "symbol": symbol,
                "ts": ts,
                "open": b.get("o"),
                "high": b.get("h"),
                "low": b.get("l"),
                "close": b.get("c"),
                "volume": b.get("v"),
            }
        )

    if not rows:
        return 0

    sql = """
        INSERT INTO minute_bars (
            symbol, "timestamp", open, high, low, close, volume
        )
        VALUES (
            %(symbol)s, %(ts)s, %(open)s, %(high)s, %(low)s, %(close)s, %(volume)s
        )
        ON CONFLICT (symbol, "timestamp")
        DO UPDATE SET
            open   = EXCLUDED.open,
            high   = EXCLUDED.high,
            low    = EXCLUDED.low,
            close  = EXCLUDED.close,
            volume = EXCLUDED.volume;
    """

    n = 0
    with psycopg.connect(PG_DSN) as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(sql, r)
                n += 1
        conn.commit()

    log.info(COMPONENT, "minute_bars_inserted", symbol=symbol, n=n)
    return n


def backfill_minutes_today(symbol: str) -> None:
    """Fetch and upsert today's minute bars for symbol."""
    bars = _fetch_polygon_minutes_today(symbol)
    if not bars:
        return
    _insert_minutes(symbol, bars)


def _has_minutes_today(symbol: str) -> bool:
    """Check if we have any minute bars for this symbol for 'today'."""
    if not PG_DSN:
        return False

    day = _trading_day_utc()
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    sql = """
        SELECT 1
        FROM minute_bars
        WHERE symbol = %s
          AND "timestamp" >= %s
          AND "timestamp" < %s
        LIMIT 1;
    """

    with psycopg.connect(PG_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (symbol, start, end))
            row = cur.fetchone()
            has_any = row is not None

    log.info(
        COMPONENT,
        "minute_coverage_checked",
        symbol=symbol,
        day=day.isoformat(),
        has_any=has_any,
    )
    return has_any


# ---------------------------------------------------------------------------
# Tick backfill (TAIL ONLY, today, via v3 trades)
# ---------------------------------------------------------------------------

TICK_INSERT_SQL = """
    INSERT INTO tick_data (
        symbol,
        "timestamp",
        sip_timestamp,
        participant_timestamp,
        trf_timestamp,
        price,
        size,
        exchange,
        conditions,
        tape,
        trade_id
    )
    VALUES (
        %(symbol)s,
        %(ts)s,
        %(sip_timestamp)s,
        %(participant_timestamp)s,
        %(trf_timestamp)s,
        %(price)s,
        %(size)s,
        %(exchange)s,
        %(conditions)s,
        %(tape)s,
        %(trade_id)s
    )
    ON CONFLICT (symbol, "timestamp", trade_id) DO NOTHING;
"""


def backfill_ticks_today(symbol: str) -> None:
    """Fetch and insert a *tail* of today's trades for `symbol` into tick_data.

    Design:
      * This is NOT a full-day tick backfill.
      * We only fetch up to TICK_TAIL_LIMIT most recent trades for 'today'.
      * Single-page REST call, no pagination, to avoid cross-dataset cursors.
      * Use order=desc (newest first), then reverse in-memory so inserts are asc.
      * Full historical tick backfill is expected to be done via S3/Parquet
        in a separate pipeline, not via this worker.
    """
    global TICK_BACKFILL_AVAILABLE

    if not TICK_BACKFILL_AVAILABLE:
        log.info(
            COMPONENT,
            "tick_backfill_disabled",
            symbol=symbol,
        )
        return

    if not POLYGON_KEY:
        log.warn(COMPONENT, "polygon_key_missing", symbol=symbol)
        TICK_BACKFILL_AVAILABLE = False
        return
    if not PG_DSN:
        log.error(COMPONENT, "pg_dsn_missing")
        TICK_BACKFILL_AVAILABLE = False
        return

    day = _trading_day_utc().isoformat()
    base_url = POLYGON_TRADES_V3_URL.format(symbol=symbol)

    t0 = time.time()
    total_rows = 0

    try:
        params = {
            # Constrain to today's session.
            "timestamp": day,
            # We want the most recent trades; Polygon returns desc by default
            # when order=desc, so we reverse later for ascending inserts.
            "order": "desc",
            "sort": "timestamp",
            "limit": TICK_TAIL_LIMIT,
            "apiKey": POLYGON_KEY,
        }
        resp = requests.get(base_url, params=params, timeout=10)
        try:
            resp.raise_for_status()
        except HTTPError as e:
            status = getattr(e.response, "status_code", None)
            log.error(
                COMPONENT,
                "tick_tail_http_error",
                symbol=symbol,
                date=day,
                status=status,
                url=getattr(e.response, "url", None),
                err=str(e),
            )
            return

        data = resp.json()
        raw_ticks = data.get("results") or []
        if not raw_ticks:
            log.info(
                COMPONENT,
                "tick_tail_empty",
                symbol=symbol,
                date=day,
            )
            return

        # raw_ticks are newest-first (order=desc). Reverse so we insert
        # oldest-first, which plays nicer with sequential readers.
        raw_ticks = list(reversed(raw_ticks))

        rows: List[Dict[str, Any]] = []
        for trow in raw_ticks:
            # v3 trades: sip_timestamp / participant_timestamp / trf_timestamp in ns.
            sip_ns = trow.get("sip_timestamp")
            part_ns = trow.get("participant_timestamp")
            trf_ns = trow.get("trf_timestamp")

            ts_ns = sip_ns or part_ns or trf_ns
            if ts_ns is None:
                continue

            ts = datetime.fromtimestamp(
                ts_ns / 1_000_000_000.0, tz=timezone.utc
            )

            price = trow.get("price")
            size = trow.get("size")
            if price is None or size is None:
                continue

            rows.append(
                {
                    "symbol": symbol,
                    "ts": ts,
                    "sip_timestamp": int(sip_ns) if sip_ns is not None else None,
                    "participant_timestamp": int(part_ns) if part_ns is not None else None,
                    "trf_timestamp": int(trf_ns) if trf_ns is not None else None,
                    "price": price,
                    "size": size,
                    "exchange": trow.get("exchange"),
                    "conditions": trow.get("conditions"),
                    "tape": trow.get("tape"),
                    "trade_id": trow.get("id"),
                }
            )

        if not rows:
            log.info(
                COMPONENT,
                "tick_tail_no_usable_rows",
                symbol=symbol,
                date=day,
            )
            return

        with psycopg.connect(PG_DSN) as conn:
            with conn.cursor() as cur:
                cur.executemany(TICK_INSERT_SQL, rows)
            conn.commit()

        total_rows = len(rows)

    except Exception as e:
        log.error(
            COMPONENT,
            "tick_tail_failed",
            symbol=symbol,
            date=day,
            err=str(e),
        )
        return

    log.info(
        COMPONENT,
        "tick_tail_inserted",
        symbol=symbol,
        date=day,
        n=total_rows,
        limit=TICK_TAIL_LIMIT,
        ms=int(1000 * (time.time() - t0)),
    )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def _process_request(ev: Dict[str, Any]) -> None:
    sym = (ev.get("symbol") or ev.get("sym") or "").upper()
    if not sym:
        return

    kinds = ev.get("kinds") or ["minute"]
    # kinds may come in as list or comma string; normalize
    if isinstance(kinds, str):
        kinds_list = [k.strip() for k in kinds.split(",") if k.strip()]
    else:
        kinds_list = list(kinds)

    kinds_set = set(kinds_list)

    scope = ev.get("scope") or "today"
    who = ev.get("requested_by") or "unknown"

    if scope != "today":
        log.warn(COMPONENT, "unsupported_scope", symbol=sym, scope=scope)
        return

    log.info(
        COMPONENT,
        "backfill_request_start",
        symbol=sym,
        kinds=",".join(sorted(kinds_set)),
        requested_by=who,
    )

    try:
        # WATCH path: only "minute" in kinds
        if kinds_set == {"minute"}:
            backfill_minutes_today(sym)

        # WARM path: "minute" + "tick"
        elif "tick" in kinds_set:
            # Your rule: minutes must exist before ticks.
            if "minute" in kinds_set and not _has_minutes_today(sym):
                backfill_minutes_today(sym)
            # But ticks are *tail only* – just enough to warm the model.
            backfill_ticks_today(sym)

        else:
            log.warn(
                COMPONENT,
                "backfill_kinds_ignored",
                symbol=sym,
                kinds=",".join(sorted(kinds_set)),
            )
    except Exception as e:
        log.error(COMPONENT, "backfill_request_failed", symbol=sym, err=str(e))
    else:
        log.info(
            COMPONENT,
            "backfill_request_complete",
            symbol=sym,
            kinds=",".join(sorted(kinds_set)),
            requested_by=who,
        )


async def run_backfill_worker() -> None:
    ps = await subscribe(CHANNELS["backfill"])
    log.info(COMPONENT, "worker_started", channel=CHANNELS["backfill"])
    loop = asyncio.get_running_loop()

    async for msg in ps.listen():
        if msg.get("type") != "message":
            continue
        ev = unpack(msg["data"])
        # Run the blocking I/O in a thread so we don't stall the bus listener.
        await loop.run_in_executor(None, _process_request, ev)


if __name__ == "__main__":
    asyncio.run(run_backfill_worker())
