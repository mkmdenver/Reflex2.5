# evaluator/filters/ross_pillars.py
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from common.bus import subscribe, unpack, publisher

ROSS_PILLARS_CHANNEL = "eval.ross_pillars"


@dataclass
class SymbolFilterState:
    symbol: str
    last_price: float = 0.0
    day_volume: int = 0
    high_of_day: float = 0.0
    open_price: Optional[float] = None


@dataclass
class RossPillarsFilter:
    """
    Shared “Ross pillars” filter.

    Reads hub ticks, maintains per-symbol state, and publishes a filtered
    stream of “interesting” symbols to a dedicated specialty queue:
        channel: eval.ross_pillars
        payload: {symbol, price, day_volume, high_of_day, ts, reason}
    """
    min_price: float = 2.0
    max_price: float = 20.0
    min_gap_pct: float = 4.0
    min_day_vol: int = 100_000

    _sym: Dict[str, SymbolFilterState] = field(default_factory=dict)

    def _state(self, sym: str) -> SymbolFilterState:
        sym = sym.upper()
        st = self._sym.get(sym)
        if not st:
            st = SymbolFilterState(symbol=sym)
            self._sym[sym] = st
        return st

    def _passes(self, st: SymbolFilterState, ev: dict) -> bool:
        price = float(ev.get("price") or ev.get("p") or st.last_price or 0.0)
        prev_close = float(ev.get("prev_close") or ev.get("pc") or 0.0)

        if price <= 0:
            return False

        if not (self.min_price <= price <= self.max_price):
            return False

        if prev_close > 0:
            gap_pct = (price - prev_close) / prev_close * 100.0
            if gap_pct < self.min_gap_pct:
                return False

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

            st.last_price = price
            st.day_volume += max(size, 0)
            st.high_of_day = max(st.high_of_day, price)

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
            await pub.publish(ROSS_PILLARS_CHANNEL, payload)


async def run() -> None:
    filt = RossPillarsFilter()
    await filt.run()


if __name__ == "__main__":
    asyncio.run(run())
