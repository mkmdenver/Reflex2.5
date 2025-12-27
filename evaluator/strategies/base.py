# evaluator/strategies/base.py
from __future__ import annotations
from typing import Optional

class Decision:  # if you already have this, remove this shim
    ...

class StrategyBase:
    # ... other methods/init ...

    def on_trade(self, symbol: str, price: float, ts_ns: int) -> Optional[Decision]:
        """
        Default is a no-op signal: return None (no decision).
        Concrete strategies should override.
        """
        return None

    def reset(self, symbol: str) -> None:
        """
        Default reset does nothing—safe and operational.
        Concrete strategies may clear internal state here.
        """
        return None
