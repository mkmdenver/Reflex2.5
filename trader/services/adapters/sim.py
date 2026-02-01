# trader/adapters/sim.py
from __future__ import annotations
from ..portfolio_core import AccountSnapshot
from .base import BrokerAdapter

class SimAdapter(BrokerAdapter):
    def __init__(self, account_id: str, label: str) -> None:
        self.account_id = account_id
        self.label = label

    async def snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(
            account_id=self.account_id,
            cash=0.0,
            equity=0.0,
            buying_power=0.0,
            positions=[],
        )
