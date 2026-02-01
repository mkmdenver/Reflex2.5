# trader/portfolio_core.py
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Dict, List

@dataclass
class Position:
    symbol: str
    qty: float
    avg_price: float
    market_price: float
    def to_dict(self) -> dict: return asdict(self)

@dataclass
class AccountSnapshot:
    account_id: str
    cash: float
    equity: float
    buying_power: float
    positions: List[Position]
    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "cash": float(self.cash),
            "equity": float(self.equity),
            "buying_power": float(self.buying_power),
            "positions": [p.to_dict() for p in self.positions],
        }

@dataclass
class PortfolioTotals:
    cash: float
    equity: float
    buying_power: float
    def to_dict(self) -> dict: return asdict(self)

def compute_totals(snapshots: Dict[str, AccountSnapshot]) -> PortfolioTotals:
    cash = sum(s.cash for s in snapshots.values())
    equity = sum(s.equity for s in snapshots.values())
    bp = sum(s.buying_power for s in snapshots.values())
    return PortfolioTotals(cash=cash, equity=equity, buying_power=bp)
