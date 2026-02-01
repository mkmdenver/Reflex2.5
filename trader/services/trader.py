# trader/types.py
# v1.0 — Shared lightweight dataclasses used by adapters and portfolio manager

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Dict, Any


@dataclass
class Position:
    symbol: str
    qty: float
    avg_price: float
    market_price: float = 0.0
    side: str = ""  # "long" / "short" if your broker distinguishes

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "qty": self.qty,
            "avg_price": self.avg_price,
            "market_price": self.market_price,
            "side": self.side,
        }


@dataclass
class AccountSnapshot:
    account_id: str
    cash: float
    equity: float
    buying_power: float
    positions: List[Position] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "cash": self.cash,
            "equity": self.equity,
            "buying_power": self.buying_power,
            "positions": [p.to_dict() for p in self.positions],
        }
