# trader/adapters/types.py
# v1.0 — minimal types used by adapters & API.

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

@dataclass
class Position:
    symbol: str
    qty: float
    avg_price: float

    def to_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "qty": self.qty, "avg_price": self.avg_price}

@dataclass
class AccountSnapshot:
    account_id: str
    cash: float
    equity: float
    buying_power: float
    positions: List[Position] = field(default_factory=list)
    updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "cash": float(self.cash or 0),
            "equity": float(self.equity or 0),
            "buying_power": float(self.buying_power or 0),
            "positions": [p.to_dict() if hasattr(p, "to_dict") else p for p in self.positions],
            "updated_at": self.updated_at,
        }
