# trader/portfolio.py
from __future__ import annotations
from typing import Dict
from .portfolio_core import AccountSnapshot, PortfolioTotals, compute_totals

class Portfolio:
    def __init__(self) -> None:
        self.instance: str | None = None
        self.updated_ts: float = 0.0
        self.updated_ts_ns: int = 0
        self.accounts: Dict[str, AccountSnapshot] = {}
        self.totals: PortfolioTotals | None = None
        self.risk = {
            "portfolio_stop_tripped": False,
            "portfolio_stop_reason": "",
            "constraints": {},
        }

    def update_from_adapters(self, snapshots: Dict[str, AccountSnapshot]) -> None:
        self.accounts = snapshots
        self.totals = compute_totals(snapshots)

    def to_dict(self) -> dict:
        return {
            "instance": self.instance,
            "updated_ts": self.updated_ts,
            "updated_ts_ns": self.updated_ts_ns,
            "accounts": {k: v.to_dict() for k, v in self.accounts.items()},
            "totals": (self.totals.to_dict() if self.totals else {}),
            "risk": self.risk,
        }
