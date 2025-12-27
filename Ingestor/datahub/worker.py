"""
Reflex DataHub worker.

Single process that:

- Connects to the live or replay market data adapter
- Routes ticks / quotes into the in-memory DataHub registry and out to Redis
- Listens for tier change requests from engineering tools
- Periodically recomputes Polygon subscriptions from the in-memory tiers
- Exposes /internal/health for observability
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Tuple

from flask import Flask, jsonify

from common.logging import get_logger
from common.bus import subscribe, CHANNELS, unpack, publish_async

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
# Tier request listener – Redis only, no DB
# ---------------------------------------------------------------------------

class TierRequestListener:
    """
    Listens on the Redis tier request channel and updates the in-memory tier map.
    Also kicks a backfill request for upgrades into WARM/HOT.
    """

    def __init__(self, channel: str) -> None:
        self._channel = channel

    async def run(self) -> None:
        log.info("tier_requests.listen.start channel=%s", self._channel)
        ps = await subscribe(self._channel)
        async for msg in ps.listen():
            if msg.get("type") != "message":
                continue
            try:
                payload = unpack(msg["data"])
            except Exception as e:
                _note_error(f"tier_request.unpack_error: {e!r}")
                continue

            symbol = payload.get("symbol")
            tier = payload.get("tier")
            source = payload.get("source", "unknown")

            if not symbol or not tier:
                continue

            old_tier, new_tier = set_symbol_tier(symbol, tier)

            log.info(
                "tier_request.applied symbol=%s tier=%s old_tier=%s source=%s",
                symbol,
                new_tier,
                old_tier,
                source,
            )

            # Only trigger backfill when moving up into WARM/HOT
            if (
                TIER_RANK[new_tier] >= TIER_RANK["WARM"]
                and TIER_RANK[new_tier] > TIER_RANK[old_tier]
            ):
                backfill_req = {
                    "symbol": symbol.upper(),
                    "kinds": ["minute", "tick"],
                    "scope": "today",
                    "requested_by": source,
                    "ts": time.time(),
                }
                try:
                    await publish_async(CHANNELS["backfill"], backfill_req)
                    log.info(
                        "backfill.requested symbol=%s kinds=%s scope=today requested_by=%s",
                        backfill_req["symbol"],
                        backfill_req["kinds"],
                        source,
                    )
                except Exception as e:
                    _note_error(f"backfill.publish_error: {e!r}")


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

    trades = sorted(set(hot + warm))
    quotes = sorted(set(hot))

    return Subscriptions(
        trades=trades,
        quotes=quotes,
        hot=hot,
        warm=warm,
        watch=watch,
        cold=cold,
    )


# ---------------------------------------------------------------------------
# Adapter factories
# ---------------------------------------------------------------------------

def _make_live_adapter():
    from datahub.adapters.live_adapter import LIVEAdapter  # type: ignore

    api_key = os.getenv("POLYGON_API_KEY")
    if not api_key:
        raise RuntimeError("POLYGON_API_KEY not set in env")

    adapter = LIVEAdapter(api_key=api_key)
    log.info(
        "live.adapter.init api_key_hint=%s",
        api_key[:4] + "…" if api_key else "NONE",
    )
    return adapter


def _make_replay_adapter(router: Router):
    from datahub.adapters.replay_adapter import ReplayAdapter  # type: ignore

    adapter = ReplayAdapter(router=router)
    log.info("replay.adapter.init")
    return adapter


def _make_adapter(hub_mode: str, router: Router):
    if hub_mode == "LIVE":
        return _make_live_adapter()
    if hub_mode == "REPLAY":
        return _make_replay_adapter(router)
    raise RuntimeError(f"Unknown REFLEX__HUB_MODE={hub_mode!r}")


# ---------------------------------------------------------------------------
# Health HTTP server
# ---------------------------------------------------------------------------

_flask_app = Flask("datahub_internal")


@_flask_app.get("/internal/health")
def internal_health():
    with _INTERNAL_LOCK:
        snapshot = dict(_INTERNAL)
        snapshot["tiers"] = dict(_INTERNAL["tiers"])
    return jsonify(snapshot)


def _run_flask_http(port: int) -> None:
    _flask_app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


# ---------------------------------------------------------------------------
# Streaming + refresh loops
# ---------------------------------------------------------------------------

async def _stream_loop(adapter, router: Router) -> None:
    """
    Run the live/replay stream in a background executor, routing events
    into the Router and updating basic tick/quote counters.
    """

    loop = asyncio.get_running_loop()

    def _run_blocking() -> None:
        # LIVEAdapter has an explicit open/start; ReplayAdapter may not.
        if hasattr(adapter, "open"):
            try:
                adapter.open()
            except Exception as e:
                _note_error(f"adapter.open.error: {e!r}")
                return
        elif hasattr(adapter, "start"):
            try:
                adapter.start()
            except Exception as e:
                _note_error(f"adapter.start.error: {e!r}")
                return

        # Stream events until the adapter stops.
        try:
            for ev in adapter.stream():
                if not isinstance(ev, dict):
                    continue

                # Polygon native events typically use ev="T"/"Q" and sym="XYZ"
                k = ev.get("ev") or ev.get("event_type") or ev.get("type")
                sym = ev.get("sym") or ev.get("symbol")
                if not sym:
                    continue

                if k in ("T", "trade"):
                    _note_tick()
                    asyncio.run_coroutine_threadsafe(
                        router.on_tick(sym, ev), loop
                    )
                elif k in ("Q", "quote"):
                    _note_quote()
                    asyncio.run_coroutine_threadsafe(
                        router.on_quote_tob(sym, ev), loop
                    )
        except Exception as e:
            _note_error(f"stream.error: {e!r}")

    await loop.run_in_executor(None, _run_blocking)


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

            # Be flexible about adapter API name + sync/async.
            fn = getattr(adapter, "update_subscriptions", None) or getattr(
                adapter, "refresh_subscriptions", None
            )
            if fn is not None:
                result = fn(subs.trades, subs.quotes)
                if asyncio.iscoroutine(result):
                    await result
        except Exception as e:
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
