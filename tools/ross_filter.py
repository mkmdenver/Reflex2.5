# tools/ross_filter.py
import os
import sys
import asyncio
import time
from typing import Dict, Any

# Make sure project root is on sys.path
ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common.bus import subscribe, unpack, publish_async, CHANNELS  # type: ignore

try:
    import requests  # type: ignore
except Exception:  # very defensive; we can still run without HTTP tier promote
    requests = None  # type: ignore


# --- Config via .env ---------------------------------------------------------

INSTANCE = os.getenv("INSTANCE", "liveA")

DATAHUB_API_BASE = os.getenv("DATAHUB_API_BASE", "http://127.0.0.1:7000")

# Very rough Ross-style filters; tweak in .env as needed
ROSS_MIN_PRICE = float(os.getenv("ROSS_MIN_PRICE", "1.0"))
ROSS_MAX_PRICE = float(os.getenv("ROSS_MAX_PRICE", "20.0"))

# Require at least this many shares traded before we consider promoting
ROSS_MIN_CUM_VOLUME = int(os.getenv("ROSS_MIN_CUM_VOLUME", "50000"))

# Require at least this many ticks seen
ROSS_MIN_TICKS = int(os.getenv("ROSS_MIN_TICKS", "50"))

# Channel that the Ross filter will publish its hits to
ROSS_STREAM = os.getenv(
    "ROSS_FILTER_CHANNEL",
    # fall back to a reasonable literal if CHANNELS doesn't define "ross"
    getattr(CHANNELS, "get", lambda *_a, **_k: None)("ross", f"reflex:{INSTANCE}:ross.warm"),
)


class SymbolState:
    __slots__ = ("promoted", "cum_volume", "tick_count", "first_ts", "last_price")

    def __init__(self, ts: float, price: float, size: int) -> None:
        self.promoted = False
        self.cum_volume = max(size, 0)
        self.tick_count = 1
        self.first_ts = ts
        self.last_price = price

    def update(self, ts: float, price: float, size: int) -> None:
        self.cum_volume += max(size, 0)
        self.tick_count += 1
        self.last_price = price


symbol_state: Dict[str, SymbolState] = {}


def extract_tick(ev: Dict[str, Any]) -> tuple[str, float, int, float]:
    """
    Normalize tick event fields coming off the bus.
    - symbol: ev["sym"] or ev["symbol"]
    - price: ev["p"] or ev["price"]
    - size: ev["s"] or ev["size"]
    - ts: ev["ts"] or time.time()
    """
    sym = (ev.get("sym") or ev.get("symbol") or "").upper()
    price = ev.get("p") or ev.get("price")
    size = ev.get("s") or ev.get("size") or 0
    ts = ev.get("ts") or time.time()

    try:
        price_f = float(price)
    except Exception:
        price_f = 0.0

    try:
        size_i = int(size)
    except Exception:
        size_i = 0

    try:
        ts_f = float(ts)
    except Exception:
        ts_f = time.time()

    return sym, price_f, size_i, ts_f


def passes_ross_filter(state: SymbolState, price: float) -> bool:
    """
    Very simplified Ross-style gate:
    - price in [ROSS_MIN_PRICE, ROSS_MAX_PRICE]
    - enough ticks seen
    - enough cumulative volume
    We only move tiers upward; no demotion logic here.
    """
    if price < ROSS_MIN_PRICE or price > ROSS_MAX_PRICE:
        return False

    if state.tick_count < ROSS_MIN_TICKS:
        return False

    if state.cum_volume < ROSS_MIN_CUM_VOLUME:
        return False

    return True


def promote_tier_http(symbol: str) -> None:
    """
    Promote symbol to WARM tier via the datahub HTTP service.
    This avoids any dependency on internal tier channels.
    """
    if requests is None:
        print(f"[ross_filter] requests not available; cannot promote {symbol} via HTTP")
        return

    url = DATAHUB_API_BASE.rstrip("/") + "/v1/tiers/service"
    payload = {"symbol": symbol, "tier": "WARM", "source": "ross_filter"}

    try:
        resp = requests.post(url, json=payload, timeout=0.5)
        if resp.ok:
            print(f"[ross_filter] promoted {symbol} -> WARM via HTTP")
        else:
            print(
                f"[ross_filter] HTTP tier promote FAILED for {symbol}: "
                f"status={resp.status_code} body={resp.text}"
            )
    except Exception as exc:
        print(f"[ross_filter] HTTP error promoting {symbol}: {exc!r}")


async def handle_tick(ev: Dict[str, Any]) -> None:
    sym, price, size, ts = extract_tick(ev)
    if not sym or price <= 0.0:
        return

    state = symbol_state.get(sym)
    if state is None:
        state = SymbolState(ts=ts, price=price, size=size)
        symbol_state[sym] = state
    else:
        state.update(ts=ts, price=price, size=size)

    if state.promoted:
        return

    if not passes_ross_filter(state, price):
        return

    # Mark as promoted first to avoid double-work
    state.promoted = True

    # 1) Raise tier via HTTP
    promote_tier_http(sym)

    # 2) Publish to the Ross specialty stream for downstream evals
    ross_event = {
        "symbol": sym,
        "price": price,
        "cum_volume": state.cum_volume,
        "tick_count": state.tick_count,
        "tier": "WARM",
        "source": "ross_filter",
        "instance": INSTANCE,
        "ts": ts,
        "reason": "ross_filter:promoted",
    }

    if ROSS_STREAM:
        print(f"[ross_filter] publishing ross hit for {sym} on {ROSS_STREAM}")
        await publish_async(ROSS_STREAM, ross_event)  # type: ignore[arg-type]
    else:
        print(f"[ross_filter] ROSS_STREAM not set; skipping publish for {sym}")


async def main() -> None:
    ps = await subscribe(CHANNELS["ticks"])
    print(
        f"[ross_filter] instance={INSTANCE} listening on {CHANNELS['ticks']} "
        f"-> ROSS_STREAM={ROSS_STREAM}"
    )

    async for msg in ps.listen():
        if msg.get("type") != "message":
            continue
        ev = unpack(msg["data"])
        try:
            await handle_tick(ev)
        except Exception as exc:
            # Keep this very lightweight; we don't want the filter to die
            print(f"[ross_filter] error handling tick: {exc!r}")


if __name__ == "__main__":
    asyncio.run(main())
