# datahub/ipc_publish.py
from __future__ import annotations

from typing import Dict, Any

from common.ipc_bus import Bus, new_trace_id, ts_utc_ns, t_mono_ns


async def publish_quote(bus: Bus, sym: str, bid: float, ask: float) -> None:
    """
    Publish a top-of-book quote snapshot to the real-time bus.
    """
    msg = {
        "trace_id": new_trace_id(),
        "ts_utc_ns": ts_utc_ns(),
        "t_mono_ns": t_mono_ns(),
        "kind": "quote",
        "payload": {
            "symbol": sym,
            "bid": bid,
            "ask": ask,
        },
    }
    await bus.publish(f"reflex:rt:quotes:{sym}", msg)


async def publish_trade(bus: Bus, sym: str, price: float, size: int) -> None:
    """
    Publish a trade (last price / size) to the real-time bus.
    """
    msg = {
        "trace_id": new_trace_id(),
        "ts_utc_ns": ts_utc_ns(),
        "t_mono_ns": t_mono_ns(),
        "kind": "trade",
        "payload": {
            "symbol": sym,
            "price": price,
            "size": size,
        },
    }
    await bus.publish(f"reflex:rt:trades:{sym}", msg)


async def publish_bar_1m(bus: Bus, bar: Dict[str, Any]) -> None:
    """
    Publish a 1-minute bar close event to the real-time bus.

    Expected `bar` shape (same as Registry.push_bar_1m):

        {
          "symbol":   "SPY",
          "t_open":   1733257200,
          "t_close":  1733257259,
          "open":     10.10,
          "high":     10.50,
          "low":      10.05,
          "close":    10.40,
          "volume":   123456,
          ...
        }

    Channel pattern: `reflex:rt:bars:1m:{symbol}`
    """
    sym = (bar.get("symbol") or "").upper()
    if not sym:
        return

    payload = {
        "symbol": sym,
        "t_open": int(bar["t_open"]),
        "t_close": int(bar["t_close"]),
        "open": float(bar["open"]),
        "high": float(bar["high"]),
        "low": float(bar["low"]),
        "close": float(bar["close"]),
        "volume": float(bar.get("volume", 0.0)),
    }

    msg = {
        "trace_id": new_trace_id(),
        "ts_utc_ns": ts_utc_ns(),
        "t_mono_ns": t_mono_ns(),
        "kind": "bar_1m",
        "payload": payload,
    }

    await bus.publish(f"reflex:rt:bars:1m:{sym}", msg)
