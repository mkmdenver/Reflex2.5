# evaluator/model_bullflag.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Literal

from evaluator.service_tiers import ModelTierState, request_service_level

TierName = Literal["COLD", "WATCH", "WARM", "HOT"]


@dataclass
class SymbolState:
    # Simple per-symbol rolling state; you can expand this later.
    last_price: float = 0.0
    high_of_day: float = 0.0
    pullback_low: float = 0.0
    in_flag: bool = False
    warm_score: float = 0.0
    hot_score: float = 0.0


class BullFlagModel:
    """
    Two-stage bull-flag strategy model living in a single Evaluator process.

    Stage A (heat filter): decide per-symbol IF this looks like a bull-flag candidate.
      - Manages its own COLD/WATCH/WARM/HOT tiers internally.
      - Calls request_service_level() when it needs WARM/HOT service from DataHub.

    Stage B (pattern): on HOT symbols, look tick-by-tick for entry conditions.
      - In this testbed version, we just log/print decisions instead of pushing to Trader.
    """

    def __init__(self, name: str = "bullflag_v1"):
        self.name = name
        self.tiers = ModelTierState(name=name)
        self.state: Dict[str, SymbolState] = {}

        # Config knobs (very rough; tune later)
        self.min_gap_pct = 3.0
        self.min_rvol = 2.0
        self.min_price = 2.0
        self.max_price = 30.0

    def _sym_state(self, symbol: str) -> SymbolState:
        symbol = symbol.upper()
        st = self.state.get(symbol)
        if not st:
            st = SymbolState()
            self.state[symbol] = st
        return st

    # ---- Stage A: heat filter (using quotes + maybe minute stats) ----

    async def on_quote_tob(self, ev: dict) -> None:
        """
        Called for each top-of-book quote (from DataHub via CHANNELS["quotes"]).

        ev example: {symbol, bid, ask, mid, vwap_1m, rvol_1m, ...}
        """
        sym = ev.get("symbol")
        if not sym:
            return
        sym = sym.upper()
        st = self._sym_state(sym)

        bid = float(ev.get("bid") or 0.0)
        ask = float(ev.get("ask") or 0.0)
        mid = (bid + ask) / 2 if bid and ask else bid or ask or 0.0
        if mid <= 0.0:
            return

        st.last_price = mid
        st.high_of_day = max(st.high_of_day, mid)

        # Basic guardrails: price range & quote sanity
        if not (self.min_price <= mid <= self.max_price):
            return

        # Rough example: treat rvol_1m and gap_pct if you decorate events with them later
        rvol = float(ev.get("rvol_1m") or 1.0)
        gap_pct = float(ev.get("gap_pct") or 0.0)

        score = 0.0
        if gap_pct >= self.min_gap_pct:
            score += 1.0
        if rvol >= self.min_rvol:
            score += 1.0
        if mid > (st.high_of_day * 0.97):  # trading near HOD
            score += 0.5

        st.warm_score = 0.9 * st.warm_score + 0.1 * score

        current = self.tiers.get(sym)

        # Model-local promotion logic:
        if current in ("COLD", "WATCH") and st.warm_score > 1.0:
            if self.tiers.maybe_raise(sym, "WARM"):
                # Ask DataHub for at least WARM service (trades + better quotes)
                request_service_level(sym, "WARM", source=self.name)

        if current in ("WARM",) and st.warm_score > 1.5:
            if self.tiers.maybe_raise(sym, "HOT"):
                # Ask DataHub for HOT service (full quotes, tick density)
                request_service_level(sym, "HOT", source=self.name)

    # ---- Stage B: pattern matcher (using trades on HOT symbols) ----

    async def on_trade(self, ev: dict) -> None:
        """
        Called for each trade (tick) event.
        Only runs the heavier bull-flag logic on model-HOT symbols.
        """
        sym = ev.get("symbol")
        if not sym:
            return
        sym = sym.upper()

        if self.tiers.get(sym) != "HOT":
            return  # ignore non-HOT symbols for Stage B

        st = self._sym_state(sym)
        price = float(ev.get("price") or 0.0)
        if price <= 0.0:
            return

        st.last_price = price
        st.high_of_day = max(st.high_of_day, price)

        # Very rough flag detection stub:
        # - watch for small, orderly pullback from HOD,
        # - then break back above a local high.
        if not st.in_flag:
            # entering potential flag: price pulls back modestly from HOD
            if price < st.high_of_day and price > st.high_of_day * 0.94:
                st.in_flag = True
                st.pullback_low = price
        else:
            st.pullback_low = min(st.pullback_low, price)
            # breakout condition: price pushes back near HOD after pullback
            if price > st.high_of_day * 0.995:
                # Here you'd normally emit an order intent.
                # For the testbed, just log it.
                print(f"[BULLFLAG] Candidate LONG {sym} at {price:.2f}")
                st.in_flag = False
