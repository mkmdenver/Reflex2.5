# evaluator/model_combo_ross_bullflag.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

from evaluator.service_tiers import request_service_level


@dataclass
class SymbolState:
    symbol: str
    last_price: float = 0.0
    high_of_day: float = 0.0
    open_price: Optional[float] = None
    day_volume: int = 0

    # crude bull-flag state
    leg_high: float = 0.0
    pullback_low: float = 0.0
    in_flag: bool = False
    triggered: bool = False

    # debug / meta
    last_reason: str = ""


@dataclass
class ComboRossBullFlagModel:
    """
    Monolithic evaluator that:

      * Applies Ross-style "pillars" to gate which symbols we care about.
      * Tracks a very simple bull-flag pattern on those symbols.
      * Requests higher service tiers when things get spicy.

    It does NOT place orders; it just decides which symbols are “worth
    attention” and when a pattern has fired.
    """
    name: str = "combo_ross_bullflag"
    min_price: float = 2.0
    max_price: float = 20.0
    min_gap_pct: float = 4.0   # e.g., +4% vs prior close
    min_rvol: float = 2.0      # relative volume threshold (if available)
    min_day_vol: int = 100_000

    _symbols: Dict[str, SymbolState] = field(default_factory=dict)

    # ------------- utils -----------------

    def _state(self, sym: str) -> SymbolState:
        sym = sym.upper()
        st = self._symbols.get(sym)
        if not st:
            st = SymbolState(symbol=sym)
            self._symbols[sym] = st
        return st

    def _passes_ross_pillars(self, st: SymbolState, ev: dict) -> bool:
        """
        Rough approximation of Ross-style gating using whatever fields
        are present in the event. The point here is the *shape*, not the
        exact magic numbers.
        """
        price = float(ev.get("price") or ev.get("p") or st.last_price or 0.0)
        prev_close = float(ev.get("prev_close") or ev.get("pc") or 0.0)
        rvol = float(ev.get("rvol") or ev.get("rvol_1m") or 0.0)

        if price <= 0.0:
            return False

        if not (self.min_price <= price <= self.max_price):
            return False

        if prev_close > 0.0:
            gap_pct = (price - prev_close) / prev_close * 100.0
            if gap_pct < self.min_gap_pct:
                return False

        if st.day_volume < self.min_day_vol:
            return False

        if rvol and rvol < self.min_rvol:
            return False

        return True

    # ------------- event handlers -----------------

    async def on_trade(self, ev: dict) -> None:
        sym = (ev.get("symbol") or ev.get("sym") or "").upper()
        if not sym:
            return

        st = self._state(sym)

        price = float(ev.get("price") or ev.get("p") or st.last_price or 0.0)
        size = int(ev.get("size") or ev.get("s") or 0)

        st.last_price = price
        st.day_volume += max(size, 0)
        st.high_of_day = max(st.high_of_day, price)

        # Ross gating
        if not self._passes_ross_pillars(st, ev):
            st.in_flag = False
            st.triggered = False
            return

        # Request WARM/HOT service level from DataHub when we care
        request_service_level(sym, tier="WARM", source=self.name)

        # Simple bull-flag pattern:
        #
        #   leg up: price makes new HOD
        #   pullback: price within ~20–40% of the leg range
        #   trigger: price reclaims near leg_high
        #
        # This is intentionally crude; your later models can be fancy.
        if price >= st.high_of_day * 0.999:
            # start or extend the impulse leg
            st.leg_high = price
            st.pullback_low = price
            st.in_flag = False
            st.triggered = False
            st.last_reason = "leg_up"
            return

        if st.leg_high <= 0.0:
            return

        # update pullback
        st.pullback_low = min(st.pullback_low or price, price)

        leg_range = st.leg_high - st.pullback_low
        if leg_range <= 0:
            return

        # check if we are in a reasonable pullback band
        pullback_pct = (st.leg_high - price) / leg_range
        if 0.2 <= pullback_pct <= 0.6:  # “flag body”
            st.in_flag = True
            st.last_reason = "in_flag"
            request_service_level(sym, tier="HOT", source=self.name)
            return

        # breakout trigger from a flag
        if st.in_flag and price >= st.leg_high * 0.995 and not st.triggered:
            st.triggered = True
            st.last_reason = "flag_breakout"
            # At this stage a higher-level component could emit an order intent.

    async def on_quote_tob(self, ev: dict) -> None:
        """
        Optional: keep quotes in case you want spread-based filters later.
        For now we just track best bid/ask so they’re handy.
        """
        sym = (ev.get("symbol") or ev.get("sym") or "").upper()
        if not sym:
            return
        st = self._state(sym)
        bid = float(ev.get("bid") or ev.get("b") or 0.0)
        ask = float(ev.get("ask") or ev.get("a") or 0.0)
        # could stash them on state if desired
        _ = bid, ask  # placeholder to avoid lint noise
