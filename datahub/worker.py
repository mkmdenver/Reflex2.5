"""
Reflex DataHub worker.

Single process that:

- Connects to the live market data adapter
- Routes ticks / quotes into the DataHub ingest router and Redis
- Listens for tier change requests from engineering tools
- Kicks off DB backfills on tier raises
- Emits a bootstrap bar+tick from DB on tier raises so filters sync quickly
- Periodically recomputes Polygon subscriptions
- Exposes /internal/health for observability
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Tuple, Optional

import psycopg
from flask import Flask, jsonify

from common.logging import get_logger
from common.bus import (
    subscribe,
    CHANNELS,
    unpack,
    publish_async,
    publisher,
    pack,
)

from datahub.ingest_router import Router
from datahub.flag_listener import FlagListener

log = get_logger("datahub")

# ---------------------------------------------------------------------------
# Config + constants (env only; no config files)
# ---------------------------------------------------------------------------

INSTANCE_ID: str = os.getenv("REFLEX__INSTANCE_ID", "hubA")
HUB_MODE: str = os.getenv("REFLEX__HUB_MODE", "LIVE").upper()

# Max concurrent symbols per tier
MAX_WATCH = int(os.getenv("DATAHUB_MAX_WATCH", "600"))
MAX_WARM = int(os.getenv("DATAHUB_MAX_WARM", "200"))
MAX_HOT = int(os.getenv("DATAHUB_MAX_HOT", "10"))

REFRESH_SEC = float(os.getenv("DATAHUB_REFRESH_SEC", "3.0"))

# Symbols that should always stay subscribed (health/atmosphere keepalive)
# Comma-separated, e.g. "SPY,QQQ". Empty means none.
DATAHUB_BOOTSTRAP_SYMBOLS = [s.strip().upper() for s in os.getenv("DATAHUB_BOOTSTRAP_SYMBOLS", "").split(",") if s.strip()]

# DB for bootstrap snapshots
DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/stock_data",
)

# Bar channel constant (must match ingest_router + FTS_simple1 + PTI_simple1)
BARS_CHANNEL = "hub.bars1m"

async def _time_tick_loop() -> None:
    # Use configured channel if present, else default name.
    ch = CHANNELS.get("time", "hub.time")
    while True:
        msg = {
            "kind": "time_tick",
            "ts_utc_ms": int(time.time() * 1000),
            "ts_utc_ns": time.time_ns(),
        }
        try:
            await publish_async(ch, msg)
        except Exception as e:
            _note_error(f"time_tick.publish_error: {e!r}")
        await asyncio.sleep(1.0)

# ---------------------------------------------------------------------------
# Internal health state
# ---------------------------------------------------------------------------

_INTERNAL_LOCK = threading.Lock()
_INTERNAL: Dict[str, Any] = {
    "instance": INSTANCE_ID,
    "mode": HUB_MODE,
    "alerts": [],
    "last_error": None,
    "last_tick_ts": 0.0,
    "last_quote_ts": 0.0,
    "last_refresh_ts": 0.0,
    "ticks_total": 0,
    "quotes_total": 0,
    "feed_ok": False,
    "feed_stale_sec": None,
    "tiers": {"COLD": 0, "WATCH": 0, "WARM": 0, "HOT": 0},
}


def _note_tick(ts_unix: float | None = None) -> None:
    if ts_unix is None:
        ts_unix = time.time()
    with _INTERNAL_LOCK:
        _INTERNAL["last_tick_ts"] = ts_unix
        _INTERNAL["ticks_total"] += 1


def _note_quote(ts_unix: float | None = None) -> None:
    if ts_unix is None:
        ts_unix = time.time()
    with _INTERNAL_LOCK:
        _INTERNAL["last_quote_ts"] = ts_unix
        _INTERNAL["quotes_total"] += 1


def _note_refresh(now: float, tier_counts: Dict[str, int]) -> None:
    with _INTERNAL_LOCK:
        _INTERNAL["last_refresh_ts"] = now
        _INTERNAL["tiers"] = dict(tier_counts)
        active = sum(tier_counts.get(k, 0) for k in ("WATCH", "WARM", "HOT"))
        _INTERNAL["feed_ok"] = active > 0
        _INTERNAL["feed_stale_sec"] = 0.0


def _note_error(msg: str) -> None:
    log.error("hub.error %s", msg)
    with _INTERNAL_LOCK:
        _INTERNAL["last_error"] = msg
        _INTERNAL["alerts"].append(msg)


# ---------------------------------------------------------------------------
# In-memory tiers
# ---------------------------------------------------------------------------

_TIER_LOCK = threading.Lock()
_TIER_BY_SYMBOL: Dict[str, str] = {}  # symbol -> "COLD"/"WATCH"/"WARM"/"HOT"

TIER_RANK = {"COLD": 0, "WATCH": 1, "WARM": 2, "HOT": 3}

# Max age (seconds) for anonymous tier events; older ones are ignored as stale
STALE_TIER_MAX_AGE_SEC = float(os.getenv("DATAHUB_TIER_MAX_AGE_SEC", "300"))

# Ignore ALL tier events for the first N seconds after startup to flush the pipe
STARTUP_IGNORE_WINDOW_SEC = float(
    os.getenv("DATAHUB_STARTUP_IGNORE_WINDOW_SEC", "1")
)

# STRICT: require ts on tier events so hub can reject pre-boot messages
REQUIRE_TIER_TS = os.getenv("DATAHUB_REQUIRE_TIER_TS", "1").lower() not in ("0", "false", "no")


def _normalize_tier(name: str) -> str:
    name = (name or "COLD").upper()
    if name not in TIER_RANK:
        return "COLD"
    return name


def set_symbol_tier(symbol: str, tier: str) -> Tuple[str, str]:
    symbol = symbol.upper()
    tier = _normalize_tier(tier)
    with _TIER_LOCK:
        old = _TIER_BY_SYMBOL.get(symbol, "COLD")
        _TIER_BY_SYMBOL[symbol] = tier
    return old, tier


def snapshot_tiers() -> List[Tuple[str, str]]:
    with _TIER_LOCK:
        return list(_TIER_BY_SYMBOL.items())


# ---------------------------------------------------------------------------
# Bootstrap from DB on tier raise
# ---------------------------------------------------------------------------


def _fetch_latest_bar_and_tick(
    symbol: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Synchronous helper: fetch latest minute bar + latest tick for symbol
    from Postgres.

    Returns (bar, tick) where:

      bar  = {"timestamp": datetime, "open":..., "high":..., "low":..., "close":..., "volume":...}
      tick = {"timestamp": datetime, "price":..., "size":...}
    """
    sym = symbol.upper()
    bar: Optional[Dict[str, Any]] = None
    tick: Optional[Dict[str, Any]] = None

    try:
        conn = psycopg.connect(DB_URL)
    except Exception:
        return None, None

    try:
        with conn.cursor() as cur:
            # Latest minute bar
            cur.execute(
                """
                SELECT "timestamp", open, high, low, close, volume
                FROM minute_bars
                WHERE symbol = %s
                ORDER BY "timestamp" DESC
                LIMIT 1
                """,
                (sym,),
            )
            row = cur.fetchone()
            if row:
                ts, o, h, l, c, v = row
                bar = {
                    "timestamp": ts,
                    "open": float(o) if o is not None else None,
                    "high": float(h) if h is not None else None,
                    "low": float(l) if l is not None else None,
                    "close": float(c) if c is not None else None,
                    "volume": float(v) if v is not None else 0.0,
                }

            # Latest tick
            cur.execute(
                """
                SELECT "timestamp", price, size
                FROM tick_data
                WHERE symbol = %s
                ORDER BY "timestamp" DESC
                LIMIT 1
                """,
                (sym,),
            )
            row = cur.fetchone()
            if row:
                ts, price, size = row
                tick = {
                    "timestamp": ts,
                    "price": float(price) if price is not None else None,
                    "size": int(size) if size is not None else 0,
                }
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return bar, tick


async def _emit_bootstrap_from_db(symbol: str) -> None:
    """
    Async wrapper: run the DB fetch in a thread, then publish a bootstrap
    1m bar on hub.bars1m and a bootstrap tick on CHANNELS["ticks"].
    """
    sym = symbol.upper()

    bar, tick = await asyncio.to_thread(_fetch_latest_bar_and_tick, sym)
    if not bar and not tick:
        return

    pub = await publisher()

    # Bootstrap bar
    if bar and bar.get("open") is not None:
        ts = bar["timestamp"]
        if isinstance(ts, datetime):
            dt = ts
        else:
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)

        minute_key = int(dt.timestamp() // 60)

        msg_bar = {
            "sym": sym,
            "minute_key": minute_key,
            "o": bar["open"],
            "h": bar["high"],
            "l": bar["low"],
            "c": bar["close"],
            "v": bar.get("volume", 0.0),
            "t_recv_ns": time.time_ns(),
        }
        try:
            await pub.publish(BARS_CHANNEL, pack(msg_bar))
            log.info(
                "hub.bootstrap_bar",
                extra={"symbol": sym, "minute_key": minute_key, "open": bar["open"]},
            )
        except Exception as e:  # noqa: BLE001
            _note_error(f"bootstrap_bar.publish_error: {e!r}")

    # Bootstrap tick
    if tick and tick.get("price") is not None:
        msg_tick = {
            "symbol": sym,
            "price": float(tick["price"]),
            "size": int(tick.get("size") or 0),
        }
        try:
            await pub.publish(CHANNELS["ticks"], pack(msg_tick))
            log.info(
                "hub.bootstrap_tick",
                extra={"symbol": sym, "price": msg_tick["price"], "size": msg_tick["size"]},
            )
        except Exception as e:  # noqa: BLE001
            _note_error(f"bootstrap_tick.publish_error: {e!r}")


# ---------------------------------------------------------------------------
# Tier request listener – Redis + bootstrap
# ---------------------------------------------------------------------------


class TierRequestListener:
    """
    Listens on the Redis tier request channel and updates the in-memory tier map.
    Also:

      - Kicks backfill requests when symbols move up in tier.
      - Emits a bootstrap bar+tick from DB so filters sync quickly.
    """

    def __init__(self, channel: str) -> None:
        self._channel = channel
        self._boot_ts = time.time()

    async def run(self) -> None:
        log.info("tier_requests.listen.start channel=%s boot_ts=%.6f", self._channel, self._boot_ts)
        ps = await subscribe(self._channel)

        log.info("tier_requests.time_tick_loop.start")
        asyncio.create_task(_time_tick_loop())


        async for msg in ps.listen():
            if msg.get("type") != "message":
                continue

            try:
                payload = unpack(msg["data"])
            except Exception as e:  # noqa: BLE001
                _note_error(f"tier_request.unpack_error: {e!r}")
                continue

            symbol = payload.get("symbol")
            tier = payload.get("tier")
            source = payload.get("source", "unknown")
            ts = payload.get("ts")

            if not symbol or not tier:
                continue

            symbol = str(symbol).upper()
            tier = _normalize_tier(str(tier))
            now = time.time()

            # 0) Startup ignore window (flush pipe / race protection)
            since_start = now - self._boot_ts
            if since_start < STARTUP_IGNORE_WINDOW_SEC:
                log.info(
                    "tier_request.ignored_startup_window symbol=%s tier=%s source=%s age_since_start=%.3f window=%.3f",
                    symbol, tier, source, since_start, STARTUP_IGNORE_WINDOW_SEC
                )
                continue

            # 1) Require ts (strict mode) so we can enforce pre-boot rejection
            if REQUIRE_TIER_TS and ts is None:
                log.info(
                    "tier_request.ignored_no_ts symbol=%s tier=%s source=%s require_ts=%s",
                    symbol, tier, source, REQUIRE_TIER_TS
                )
                continue

            ts_val: Optional[float] = None
            if ts is not None:
                try:
                    ts_val = float(ts)
                except Exception:
                    ts_val = None

            # If strict and ts couldn't parse, ignore
            if REQUIRE_TIER_TS and ts_val is None:
                log.info(
                    "tier_request.ignored_bad_ts symbol=%s tier=%s source=%s ts=%r",
                    symbol, tier, source, ts
                )
                continue

            # 2) Hard rule: ignore anything from before this DataHub instance booted
            if ts_val is not None and ts_val < self._boot_ts:
                log.info(
                    "tier_request.ignored_pre_boot symbol=%s tier=%s source=%s ts=%.6f boot_ts=%.6f",
                    symbol, tier, source, ts_val, self._boot_ts
                )
                continue

            # 3) For unknown sources, enforce stale cutoff
            if ts_val is not None and source == "unknown":
                age = now - ts_val
                if age > STALE_TIER_MAX_AGE_SEC:
                    log.info(
                        "tier_request.ignored_stale symbol=%s tier=%s source=%s age=%.2f max_age=%.2f",
                        symbol, tier, source, age, STALE_TIER_MAX_AGE_SEC
                    )
                    continue

            # Apply
            old_tier, new_tier = set_symbol_tier(symbol, tier)
            log.info(
                "tier_request.applied symbol=%s tier=%s old_tier=%s source=%s",
                symbol, new_tier, old_tier, source
            )

            # Only trigger side effects on TRUE raise
            if TIER_RANK[new_tier] <= TIER_RANK[old_tier]:
                continue

            # Decide backfill kinds
            kinds: list[str] = []
            if TIER_RANK[new_tier] >= TIER_RANK["WATCH"]:
                kinds.append("minute")
            if TIER_RANK[new_tier] >= TIER_RANK["WARM"]:
                kinds.append("tick")
            if not kinds:
                continue

            backfill_req = {
                "symbol": symbol,
                "kinds": kinds,
                "scope": "today",
                "requested_by": source,
                "ts": time.time(),
            }
            try:
                await publish_async(CHANNELS["backfill"], backfill_req)
                log.info(
                    "backfill.requested symbol=%s kinds=%s scope=today requested_by=%s",
                    backfill_req["symbol"], backfill_req["kinds"], source
                )
            except Exception as e:  # noqa: BLE001
                _note_error(f"backfill.publish_error: {e!r}")

            # Fire bootstrap
            try:
                asyncio.create_task(_emit_bootstrap_from_db(symbol))
            except RuntimeError:
                await _emit_bootstrap_from_db(symbol)


# ---------------------------------------------------------------------------
# Subscription computation
# ---------------------------------------------------------------------------


@dataclass
class Subscriptions:
    trades: List[str]
    quotes: List[str]
    hot: List[str]
    warm: List[str]
    watch: List[str]
    cold: List[str]


def compute_subscriptions(rows: Iterable[Tuple[str, str]]) -> Subscriptions:
    hot: List[str] = []
    warm: List[str] = []
    watch: List[str] = []
    cold: List[str] = []

    for sym, tier_name in rows:
        sym = sym.upper()
        t = _normalize_tier(tier_name)

        if t == "HOT":
            if len(hot) < MAX_HOT:
                hot.append(sym)
        elif t == "WARM":
            if len(warm) < MAX_WARM:
                warm.append(sym)
        elif t == "WATCH":
            if len(watch) < MAX_WATCH:
                watch.append(sym)
        else:
            cold.append(sym)

    trades = sorted(set(hot + warm + DATAHUB_BOOTSTRAP_SYMBOLS))
    quotes = sorted(set(hot + DATAHUB_BOOTSTRAP_SYMBOLS))

    return Subscriptions(
        trades=trades,
        quotes=quotes,
        hot=hot,
        warm=warm,
        watch=watch,
        cold=cold,
    )


# ---------------------------------------------------------------------------
# Adapter factories (LIVE only; REPLAY disabled here)
# ---------------------------------------------------------------------------


def _make_live_adapter():
    from datahub.adapters.live_adapter import LIVEAdapter  # type: ignore

    api_key = os.getenv("POLYGON_API_KEY")
    if not api_key:
        raise RuntimeError("POLYGON_API_KEY not set in env")

    ws_url = os.getenv("POLYGON_WS_URL") or None

    adapter = LIVEAdapter(api_key=api_key, ws_url=ws_url)
    log.info("live.adapter.init api_key_hint=%s", api_key[:4] + "…" if api_key else "NONE")

    adapter.open()
    return adapter


def _make_adapter(hub_mode: str, router: Router):  # noqa: ARG001
    mode = (hub_mode or "LIVE").upper()
    if mode != "LIVE":
        raise RuntimeError(f"REPLAY mode is disabled in datahub.worker; REFLEX__HUB_MODE={mode!r}")
    return _make_live_adapter()


# ---------------------------------------------------------------------------
# Health HTTP server
# ---------------------------------------------------------------------------

_flask_app = Flask("datahub_internal")


@_flask_app.get("/internal/health")
def internal_health():
    with _INTERNAL_LOCK:
        now = time.time()
        last_tick = _INTERNAL.get("last_tick_ts") or 0.0
        last_quote = _INTERNAL.get("last_quote_ts") or 0.0
        _INTERNAL["feed_stale_sec"] = float(max(0.0, now - max(last_tick, last_quote)))
        return jsonify(_INTERNAL)


def _run_flask_http(port: int) -> None:
    from waitress import serve

    log.info("internal_http.start port=%d", port)
    serve(_flask_app, host="0.0.0.0", port=port)


# ---------------------------------------------------------------------------
# Stream loop
# ---------------------------------------------------------------------------

_DEBUG_STREAM_SEEN = 0


async def _handle_stream_message(msg: Dict[str, Any], router: Router) -> None:
    global _DEBUG_STREAM_SEEN
    try:
        if not isinstance(msg, dict):
            return

        ev_type = msg.get("ev") or msg.get("event_type") or msg.get("type")
        sym = msg.get("sym") or msg.get("symbol")
        if not ev_type or not sym:
            return

        ev_type_str = str(ev_type)
        sym_str = str(sym)

        if _DEBUG_STREAM_SEEN < 10:
            _DEBUG_STREAM_SEEN += 1
            log.info("stream.raw ev=%s sym=%s keys=%s", ev_type_str, sym_str, sorted(msg.keys()))

        ev_norm = ev_type_str.upper()

        if ev_norm.startswith("T") or ev_norm == "TRADE":
            _note_tick()
            await router.on_tick(sym_str, msg)
        elif ev_norm.startswith("Q") or ev_norm == "QUOTE":
            _note_quote()
            await router.on_quote_tob(sym_str, msg)
        else:
            return
    except Exception as e:  # noqa: BLE001
        _note_error(f"stream_loop.error: {e!r}")


async def _stream_loop(adapter, router: Router) -> None:
    stream = adapter.stream()
    loop = asyncio.get_running_loop()

    if hasattr(stream, "__aiter__"):
        async for msg in stream:
            await _handle_stream_message(msg, router)
        return

    def _sync_consume():
        for msg in stream:
            asyncio.run_coroutine_threadsafe(_handle_stream_message(msg, router), loop)

    await loop.run_in_executor(None, _sync_consume)


# ---------------------------------------------------------------------------
# Refresh loop – recompute subscriptions periodically
# ---------------------------------------------------------------------------


async def _refresh_loop(adapter) -> None:
    while True:
        start = time.time()
        try:
            rows = snapshot_tiers()
            subs = compute_subscriptions(rows)

            tier_counts = {
                "COLD": len(subs.cold),
                "WATCH": len(subs.watch),
                "WARM": len(subs.warm),
                "HOT": len(subs.hot),
            }
            _note_refresh(start, tier_counts)

            log.info(
                "refresh.subscriptions trades=%d quotes=%d hot=%d warm=%d watch=%d cold=%d",
                len(subs.trades),
                len(subs.quotes),
                len(subs.hot),
                len(subs.warm),
                len(subs.watch),
                len(subs.cold),
            )


            fn = getattr(adapter, "update_subscriptions", None) or getattr(adapter, "refresh_subscriptions", None)
            if fn is not None:
                result = fn(subs.trades, subs.quotes)
                if asyncio.iscoroutine(result):
                    await result
        except Exception as e:  # noqa: BLE001
            _note_error(f"refresh_loop.error: {e!r}")

        elapsed = time.time() - start
        await asyncio.sleep(max(REFRESH_SEC - elapsed, 0.1))


# ---------------------------------------------------------------------------
# Main async graph
# ---------------------------------------------------------------------------


async def _run_async() -> None:
    router = Router()
    adapter = _make_adapter(HUB_MODE, router)

    flag_listener = FlagListener()
    tier_listener = TierRequestListener(CHANNELS["raise"])

    internal_port = int(os.getenv("DATAHUB_INTERNAL_PORT", "7070"))
    http_thread = threading.Thread(
        target=_run_flask_http,
        args=(internal_port,),
        name="datahub-internal-http",
        daemon=True,
    )
    http_thread.start()

    log.info("boot instance=%s mode=%s", INSTANCE_ID, HUB_MODE)

    await asyncio.gather(
        router.start(),
        _stream_loop(adapter, router),
        _refresh_loop(adapter),
        flag_listener.run(),
        tier_listener.run(),
    )


def main() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_async())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
