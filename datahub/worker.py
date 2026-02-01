# datahub/worker.py
from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from common.bus import CHANNELS, subscribe, unpack
from datahub.ingest_router import Router

# NOTE: This worker is LIVE-only in your current architecture.
HUB_MODE = "LIVE"

DATAHUB_BOOTSTRAP_SYMBOLS = [
    s.strip().upper()
    for s in (os.getenv("DATAHUB_BOOTSTRAP_SYMBOLS", "") or "").split(",")
    if s.strip()
]

# ----------------------------
# Tier state (current version)
# ----------------------------

_TIER_LOCK = threading.Lock()
_TIER_BY_SYMBOL: Dict[str, str] = {}  # symbol -> "COLD"/"WATCH"/"WARM"/"HOT"


def _get_symbol_tier(sym: str) -> str:
    sym = (sym or "").upper().strip()
    if not sym:
        return "COLD"
    with _TIER_LOCK:
        return _TIER_BY_SYMBOL.get(sym, "COLD")


TIER_RANK = {"COLD": 0, "WATCH": 1, "WARM": 2, "HOT": 3}


def _tier_ge(a: str, b: str) -> bool:
    return TIER_RANK.get(a, 0) >= TIER_RANK.get(b, 0)


# ----------------------------
# Stats
# ----------------------------

_STATS_TICKS_RX = 0
_STATS_QUOTES_RX = 0
_STATS_BARS_RX = 0
_STATS_ERRORS = 0
_DEBUG_STREAM_SEEN = 0
_LAST_EVENT_TS = 0.0


def _note_tick() -> None:
    global _STATS_TICKS_RX, _LAST_EVENT_TS
    _STATS_TICKS_RX += 1
    _LAST_EVENT_TS = time.time()


def _note_quote() -> None:
    global _STATS_QUOTES_RX, _LAST_EVENT_TS
    _STATS_QUOTES_RX += 1
    _LAST_EVENT_TS = time.time()


def _note_bar() -> None:
    global _STATS_BARS_RX, _LAST_EVENT_TS
    _STATS_BARS_RX += 1
    _LAST_EVENT_TS = time.time()


def _note_error(_s: str) -> None:
    global _STATS_ERRORS
    _STATS_ERRORS += 1


# ----------------------------
# Subscriptions
# ----------------------------

@dataclass
class Subscriptions:
    trades: List[str]
    quotes: List[str]
    bars_1m: List[str]
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
        tn = tier_name.upper()
        if tn == "HOT":
            hot.append(sym)
        elif tn == "WARM":
            warm.append(sym)
        elif tn == "WATCH":
            watch.append(sym)
        else:
            cold.append(sym)

    # Your tier policy:
    # WATCH => AM 1m bars (official bars)
    # WARM  => ticks (trades)
    # HOT   => quotes/L3 (quotes)
    trades = sorted(set(hot + warm + DATAHUB_BOOTSTRAP_SYMBOLS))
    quotes = sorted(set(hot + DATAHUB_BOOTSTRAP_SYMBOLS))
    bars_1m = sorted(set(hot + warm + watch + DATAHUB_BOOTSTRAP_SYMBOLS))

    return Subscriptions(
        trades=trades,
        quotes=quotes,
        bars_1m=bars_1m,
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

    api_key = os.getenv("POLYGON_API_KEY") or os.getenv("POLYGON_KEY") or ""
    ws_url = os.getenv("POLYGON_WS_URL") or None
    return LIVEAdapter(api_key=api_key, ws_url=ws_url)


def _make_adapter(mode: str, _router: Router):
    if mode != "LIVE":
        raise RuntimeError("DataHub worker is LIVE-only in this build")
    a = _make_live_adapter()
    a.open()
    return a


# ---------------------------------------------------------------------------
# Tier request listener (simple)
# ---------------------------------------------------------------------------

class TierRequestListener:
    def __init__(self, channel: str) -> None:
        self.channel = channel

    async def run(self) -> None:
        ps = await subscribe(self.channel)
        print(f"[datahub.worker] tier listener subscribed: {self.channel}")
        while True:
            msg = await ps.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if not msg:
                await asyncio.sleep(0.05)
                continue
            try:
                evt = unpack(msg["data"])
            except Exception:
                continue
            if not isinstance(evt, dict):
                continue
            sym = (evt.get("symbol") or evt.get("sym") or "").upper().strip()
            tier = (evt.get("tier") or "").upper().strip()
            if not sym or not tier:
                continue
            if tier not in ("COLD", "WATCH", "WARM", "HOT"):
                continue
            with _TIER_LOCK:
                _TIER_BY_SYMBOL[sym] = tier


# ---------------------------------------------------------------------------
# Stream routing
# ---------------------------------------------------------------------------

async def _handle_stream_message(msg: Dict[str, Any], router: Router) -> None:
    try:
        ev_type = msg.get("ev") or msg.get("type")
        sym = msg.get("symbol") or msg.get("sym")
        if not ev_type or not sym:
            return

        ev_type_str = str(ev_type)
        sym_str = str(sym)

        global _DEBUG_STREAM_SEEN
        if _DEBUG_STREAM_SEEN < 5:
            _DEBUG_STREAM_SEEN += 1
            print(f"[datahub.worker] stream sample ev={ev_type_str} sym={sym_str} keys={sorted(msg.keys())}")

        ev_norm = ev_type_str.upper()

        if ev_norm.startswith("T") or ev_norm == "TRADE":
            _note_tick()
            await router.on_tick(sym_str, msg)
        elif ev_norm.startswith("Q") or ev_norm == "QUOTE":
            _note_quote()
            await router.on_quote_tob(sym_str, msg)
        elif ev_norm in ("AM", "BAR_1M", "BAR1M", "AGG_1M") or ev_norm.startswith("AM"):
            _note_bar()
            await router.on_bar_1m(sym_str, msg)

    except Exception as e:  # noqa: BLE001
        _note_error(f"stream_loop.error: {e!r}")


async def _stream_loop(adapter, router: Router) -> None:
    stream = adapter.stream()

    # async generator?
    if hasattr(stream, "__aiter__"):
        async for msg in stream:
            await _handle_stream_message(msg, router)
        return

    # sync generator -> consume in executor
    loop = asyncio.get_running_loop()

    def _sync_consume():
        for msg in stream:
            asyncio.run_coroutine_threadsafe(_handle_stream_message(msg, router), loop)

    await loop.run_in_executor(None, _sync_consume)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _run_async() -> None:
    # Start banner
    print("[datahub.worker] START")
    print(f"[datahub.worker] HUB_MODE={HUB_MODE}")
    print(f"[datahub.worker] CHANNEL raise={CHANNELS.get('raise')}")
    print(f"[datahub.worker] CHANNEL bars1m_live={CHANNELS.get('bars1m_live', CHANNELS.get('bars1m'))}")
    print(f"[datahub.worker] CHANNEL ticks_live={CHANNELS.get('ticks_live', CHANNELS.get('ticks'))}")
    print(f"[datahub.worker] CHANNEL quotes_live={CHANNELS.get('quotes_live', CHANNELS.get('quotes'))}")
    print(f"[datahub.worker] bootstrap_symbols={DATAHUB_BOOTSTRAP_SYMBOLS}")

    # Quick sanity: missing key means ws connects but never auths successfully
    if not (os.getenv("POLYGON_API_KEY") or os.getenv("POLYGON_KEY")):
        print("[datahub.worker] WARNING: POLYGON_API_KEY is missing (no live data will arrive)")

    router = Router()
    adapter = _make_adapter(HUB_MODE, router)

    tier_listener = TierRequestListener(CHANNELS["raise"])

    try:
        router.set_tier_getter(_get_symbol_tier)
    except Exception:
        pass

    await router.start()

    async def refresh_loop():
        # Minimal subscription refresh loop
        last_print = 0.0
        last_subs: Optional[Tuple[int, int, int]] = None

        while True:
            with _TIER_LOCK:
                rows = list(_TIER_BY_SYMBOL.items())

            subs = compute_subscriptions(rows)

            fn = getattr(adapter, "update_subscriptions", None) or getattr(adapter, "refresh_subscriptions", None)
            if fn is not None:
                try:
                    result = fn(subs.trades, subs.quotes, subs.bars_1m)
                except TypeError:
                    result = fn(subs.trades, subs.quotes)
                if asyncio.iscoroutine(result):
                    await result

            # heartbeat print every ~2s
            now = time.time()
            if now - last_print >= 2.0:
                last_print = now
                tiers = {"HOT": 0, "WARM": 0, "WATCH": 0, "COLD": 0}
                for _, t in rows:
                    tiers[t.upper() if t else "COLD"] = tiers.get(t.upper(), 0) + 1

                sub_sig = (len(subs.trades), len(subs.quotes), len(subs.bars_1m))
                sub_changed = (sub_sig != last_subs)
                last_subs = sub_sig

                idle = (now - _LAST_EVENT_TS) if _LAST_EVENT_TS else None
                idle_s = f"{idle:.1f}s" if idle is not None else "-"

                print(
                    "[datahub.worker] hb "
                    f"tiers(H={tiers.get('HOT',0)} Wm={tiers.get('WARM',0)} Wa={tiers.get('WATCH',0)} C={tiers.get('COLD',0)}) "
                    f"subs(T={sub_sig[0]} Q={sub_sig[1]} AM={sub_sig[2]}{'*' if sub_changed else ''}) "
                    f"rx(T={_STATS_TICKS_RX} Q={_STATS_QUOTES_RX} AM={_STATS_BARS_RX} err={_STATS_ERRORS}) "
                    f"idle={idle_s}"
                )

            await asyncio.sleep(0.5)

    await asyncio.gather(
        tier_listener.run(),
        refresh_loop(),
        _stream_loop(adapter, router),
    )


def main() -> None:
    asyncio.run(_run_async())


if __name__ == "__main__":
    main()
