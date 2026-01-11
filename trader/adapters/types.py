# trader/adapters/types.py
# v1.1 — minimal types used by adapters & API.
#
# v1.1 adds optional market fields so BrokerView can show Mkt Price / P&L
# from broker snapshots (Alpaca /v2/positions supplies these).

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


@dataclass
class Position:
    symbol: str
    qty: float
    avg_price: float

    # Optional live/broker fields (may be missing for SIM or minimal adapters)
    market_price: float = 0.0
    market_value: float = 0.0
    unrealized_pl: float = 0.0
    unrealized_plpc: float = 0.0
    side: Optional[str] = None  # "long" / "short" if provided by broker

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "symbol": self.symbol,
            "qty": float(self.qty or 0.0),
            "avg_price": float(self.avg_price or 0.0),
            "market_price": float(self.market_price or 0.0),
            "market_value": float(self.market_value or 0.0),
            "unrealized_pl": float(self.unrealized_pl or 0.0),
            "unrealized_plpc": float(self.unrealized_plpc or 0.0),
        }
        if self.side:
            d["side"] = self.side
        return d


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
