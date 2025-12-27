# datahub/ingest_router.py
from __future__ import annotations

import time
from typing import Dict, Any

from common.bus import publisher, CHANNELS, pack

import os

BARS_CHANNEL = "hub.bars1m"
# If false, DataHub will NOT synthesize 1-minute bars from ticks.
DATAHUB_BUILD_BARS_FROM_TICKS = os.getenv("DATAHUB_BUILD_BARS_FROM_TICKS", "0") in ("1","true","TRUE","yes","YES")


class Router:
    def __init__(self) -> None:
        self._pub = None
        # per-symbol bar state: sym -> {minute_key, o,h,l,c,v}
        self._bars: Dict[str, Dict[str, Any]] = {}

    async def start(self) -> None:
        self._pub = await publisher()

    async def on_tick(self, sym: str, tick: Dict[str, Any]) -> None:
        """
        Handle a trade/tick from the live adapter.

        - Fan out raw tick to CHANNELS["ticks"] (as before)
        - Feed it into a very simple 1m bar builder and emit bars on BARS_CHANNEL
        """
        if self._pub is None:
            return

        tick["t_recv_ns"] = time.time_ns()
        await self._pub.publish(CHANNELS["ticks"], pack(tick))

        if DATAHUB_BUILD_BARS_FROM_TICKS:
            await self._on_bar_from_tick(sym, tick)

    async def on_quote_tob(self, sym: str, q: Dict[str, Any]) -> None:
        """
        Handle a top-of-book quote.

        - Fan out to CHANNELS["quotes"] (unchanged)
        """
        if self._pub is None:
            return

        q["t_recv_ns"] = time.time_ns()
        await self._pub.publish(CHANNELS["quotes"], pack(q))

    # ------------------------------------------------------------------
    # Internal: build 1-minute bars from ticks
    # ------------------------------------------------------------------

    async def _on_bar_from_tick(self, sym: str, tick: Dict[str, Any]) -> None:
        """
        Extremely simple 1-minute bar builder based on wall-clock time.

        Goal:
          - centralize bar-building in DataHub
          - push canonical 1m OHLCV updates on BARS_CHANNEL

        Later you can swap this out for Polygon's official bar stream.
        """
        if self._pub is None:
            return

        # Try common Polygon trade shapes
        price = (
            tick.get("p")
            or tick.get("price")
            or tick.get("last_price")
            or tick.get("c")
        )
        if price is None:
            return

        try:
            price = float(price)
        except Exception:
            return

        size = tick.get("s") or tick.get("size") or tick.get("q") or 0
        try:
            size = int(size)
        except Exception:
            size = 0

        now_sec = time.time()
        minute_key = int(now_sec // 60)  # coarse bucket: 1-minute slices

        state = self._bars.get(sym)

        # First bar or rollover to a new minute
        if state is None or state.get("minute_key") != minute_key:
            # Flush previous bar, if we had one
            if state is not None:
                await self._emit_bar(sym, state)

            self._bars[sym] = {
                "minute_key": minute_key,
                "o": price,
                "h": price,
                "l": price,
                "c": price,
                "v": size,
            }
            return

        # Update existing bar
        if price > state["h"]:
            state["h"] = price
        if price < state["l"]:
            state["l"] = price
        state["c"] = price
        state["v"] += size

    async def _emit_bar(self, sym: str, state: Dict[str, Any]) -> None:
        """
        Emit a completed 1-minute bar on BARS_CHANNEL.

        Shape:
            {
              "sym": "SPY",
              "minute_key": 12345678,
              "o": ...,
              "h": ...,
              "l": ...,
              "c": ...,
              "v": ...,
              "t_recv_ns": ...
            }
        """
        if self._pub is None:
            return

        msg = {
            "sym": sym,
            "minute_key": state["minute_key"],
            "o": state["o"],
            "h": state["h"],
            "l": state["l"],
            "c": state["c"],
            "v": state["v"],
            "t_recv_ns": time.time_ns(),
        }
        await self._pub.publish(BARS_CHANNEL, pack(msg))
