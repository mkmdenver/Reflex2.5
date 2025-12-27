# trader/adapters/alpaca_http.py
from __future__ import annotations

from typing import List
from .base import BrokerAdapter
from ..core import kvlog, log
from ..portfolio_core import AccountSnapshot, Position


class AlpacaHTTPAdapter(BrokerAdapter):
    """
    Minimal stub: logs loudly and returns zeroed snapshot (safe default).
    Replace with real HTTP calls later.
    """

    def __init__(self, account_id: str, base: str | None, api_key: str | None, api_secret: str | None) -> None:
        self.account_id = account_id
        self.base = base or "https://paper-api.alpaca.markets"
        self.api_key = api_key
        self.api_secret = api_secret
        kvlog(
            log,
            "INFO",
            "adapter.alpaca.init",
            account_id=account_id,
            base=self.base,
            key_present=bool(api_key),
            secret_present=bool(api_secret),
        )

    async def snapshot(self) -> AccountSnapshot:
        positions: List[Position] = []
        snap = AccountSnapshot(
            account_id=self.account_id,
            cash=0.0,
            equity=0.0,
            buying_power=0.0,
            positions=positions,
        )
        kvlog(log, "DEBUG", "adapter.alpaca.snapshot", account_id=self.account_id)
        return snap
