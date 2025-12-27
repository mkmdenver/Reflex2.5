
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from collections import deque

from .base import Strategy, Decision, register

def _ema(prev: Optional[float], val: float, k: float) -> float:
    return val if prev is None else (prev + k*(val - prev))

@dataclass
class _S:
    last: Optional[float] = None
    fast: Optional[float] = None
    slow: Optional[float] = None
    win: deque = field(default_factory=lambda: deque(maxlen=60))
    entry: Optional[float] = None
    position: int = 0
    peak_pnl: float = 0.0
    adds: int = 0

@register("bullflag")
class BullFlag(Strategy):
    throttle: int = 100
    torque: float = 1.0
    gear: int = 1

    def __init__(self, throttle:int=100, torque:float=1.0, gear:int=1):
        self.throttle = throttle; self.torque = torque; self.gear = gear
        self._sym: dict[str,_S] = {}

    def _s(self, sym: str) -> _S:
        s = self._sym.get(sym)
        if not s:
            s = _S()
            self._sym[sym] = s
        return s

    def reset(self, symbol: str) -> None:
        self._sym.pop(symbol, None)

    def on_trade(self, symbol: str, price: float, ts_ns: int):
        st = self._s(symbol)
        st.win.append(price)
        st.fast = _ema(st.fast, price, 2/(9+1))
        st.slow = _ema(st.slow, price, 2/(20+1))
        st.last = price

        if st.position == 0 and st.fast and st.slow and st.fast > st.slow and price >= max(st.win) * 0.999:
            st.position = self.throttle
            st.entry = price; st.adds = 0; st.peak_pnl = 0.0
            return Decision(symbol, "BUY", st.position, price, "entry")

        if st.position != 0 and st.entry is not None:
            pnl = (price - st.entry) * st.position
            st.peak_pnl = max(st.peak_pnl, pnl)
            target = 10.0 * self.gear
            stop = -5.0 * self.torque
            if pnl >= target:
                qty = st.position
                st.position = 0; st.entry = None; st.adds = 0; st.peak_pnl = 0.0
                return Decision(symbol, "SELL", qty, price, "target")
            if pnl <= stop:
                qty = st.position
                st.position = 0; st.entry = None; st.adds = 0; st.peak_pnl = 0.0
                return Decision(symbol, "SELL", qty, price, "stop")

        return None
