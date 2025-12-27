# evaluator/filters/ross_pillars.py
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from common.bus import subscribe, unpack, publisher, pack

ROSS_PILLARS_CHANNEL = "eval.ross_pillars"


@dataclass
class SymbolFilterState:
    symbol: str
    last_price: Optional[float] = None
    day_volume: int = 0
    high_of_day: float = 0.0
    prev_close: Optional[float] = None


@dataclass
class RossPillarsFilter:
    """
    Shared “Ross pillars” filter.

    Reads hub ticks, maintains per-symbol state, and publishes a filtered
    stream of “interesting” symbols to a dedicated specialty queue:

        channel: eval.ross_pillars
        payload: {
            symbol,
            price,
            day_volume,
            high_of_day,
            ts,
            reason,
        }

    This is stage 1 of the two-part eval: “who is even worth staring at?”
    A pattern matcher (stage 2) subscribes to eval.ross_pillars and
    decides when to generate intents.
    """

    min_price: float = 2.0
    max_price: float = 20.0
    min_gap_pct: float = 4.0
    min_day_vol: int = 100_000

    _sym: Dict[str, SymbolFilterState] = field(default_factory=dict)

    def _state(self, symbol: str) -> SymbolFilterState:
        symbol = symbol.upper()
        st = self._sym.get(symbol)
        if not st:
            st = SymbolFilterState(symbol=symbol)
            self._sym[symbol] = st
        return st

    def _passes(self, st: SymbolFilterState, ev: Dict[str, object]) -> bool:
        price = st.last_price or 0.0
        prev_close = st.prev_close or 0.0

        # Price band
        if not (self.min_price <= price <= self.max_price):
            return False

        # Rough gap filter, if we know prev close
        if prev_close > 0:
            gap_pct = (price - prev_close) / prev_close * 100.0
            if gap_pct < self.min_gap_pct:
                return False

        # Crude intraday volume pillar
        if st.day_volume < self.min_day_vol:
            return False

        return True

    async def run(self) -> None:
        from common.bus import CHANNELS  # import here to avoid cycles

        ps_ticks = await subscribe(CHANNELS["ticks"])
        pub = await publisher()

        async for msg in ps_ticks.listen():
            if msg.get("type") != "message":
                continue

            ev = unpack(msg["data"])
            sym = (ev.get("symbol") or ev.get("sym") or "").upper()
            if not sym:
                continue

            st = self._state(sym)
            price = float(ev.get("price") or ev.get("p") or st.last_price or 0.0)
            size = int(ev.get("size") or ev.get("s") or 0)
            prev_close = float(ev.get("prev_close") or ev.get("pc") or st.prev_close or 0.0)

            st.last_price = price
            st.day_volume += max(size, 0)
            st.high_of_day = max(st.high_of_day, price)
            st.prev_close = prev_close or st.prev_close

            if not self._passes(st, ev):
                continue

            payload = {
                "symbol": sym,
                "price": price,
                "day_volume": st.day_volume,
                "high_of_day": st.high_of_day,
                "ts": time.time_ns(),
                "reason": "ross_pillars_pass",
            }
            # Use msgpack over pub/sub to match the rest of common.bus
            await pub.publish(ROSS_PILLARS_CHANNEL, pack(payload))


async def run() -> None:
    filt = RossPillarsFilter()
    await filt.run()


if __name__ == "__main__":
    asyncio.run(run())
