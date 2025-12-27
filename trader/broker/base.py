#{
#  "trace_id": "uuid-or-coid",
#  "payload": {
#    "account_id": "alpaca:paper:acct1",
#    "symbol": "SPY",
#    "side": "buy",                   // "buy" | "sell"
#    "order_type": "market",          // "market" | "limit" | "stop" | "stop_limit"
#    "qty": 1,
#    "time_in_force": "day",          // "day" | "gtc" | ...
#    "limit_price": 555.55,           // optional per type
#    "stop_price":  550.00,           // optional per type
#    "tags": ["ui","test"]            // optional
#  }
#}
# trader/broker/base.py
from __future__ import annotations
from typing import Any, Dict, List, Optional
from abc import ABC, abstractmethod

class Broker(ABC):
    """
    Execution adapter interface: place/replace/cancel orders; fetch open orders/positions.
    Concrete brokers (alpaca, ib, webull, schwab, sim) implement this.
    All methods are async so the worker can await network I/O.
    """

    name: str           # e.g., "alpaca"
    account_id: str     # e.g., "alpaca:paper:acct1" (optional until bound)

    def __init__(self, account_id: Optional[str] = None):
        self.account_id = account_id or ""

    @abstractmethod
    async def submit(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        """Place an order and return an ACK payload (no fabricated fills)."""
        ...

    @abstractmethod
    async def cancel(self, client_order_id: str) -> Dict[str, Any]:
        """Cancel by client order id (coid); return an ACK."""
        ...

    async def replace(self, client_order_id: str, **fields) -> Dict[str, Any]:
        """Optional: modify order if broker supports it."""
        return {"ok": False, "reason": "not_implemented"}

    @abstractmethod
    async def fetch_open_orders(self) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    async def fetch_positions(self) -> List[Dict[str, Any]]:
        ...

    async def close(self) -> None:
        """Close any HTTP/WebSocket clients if needed."""
        return
