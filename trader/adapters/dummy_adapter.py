# trader/adapters/dummy_adapter.py
from __future__ import annotations
import time
from typing import Dict, Any

class DummyAdapter:
    """
    Offline/placeholder adapter that never throws and reports stable health.
    """
    def __init__(self, broker: str, account_id: str, label: str, klass="margin"):
        self.broker = broker
        self.account_id = account_id
        self.label = label
        self.klass = klass
        self._start_ts = time.time()

    def reconcile(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "broker": self.broker,
            "label": self.label,
            "class": self.klass,
            "pdt_applies": False,
            "pdt_restricted": False,
            "allow_short": True,
            "cash": 0.0,
            "buying_power": 0.0,
            "equity": 0.0,
            "positions": [],
            "open_orders": [],
            "last_reconcile_ts": now,
            "status": "offline",
            "reason": "adapter_not_configured",
        }

    def health(self) -> Dict[str, Any]:
        return {
            "status": "offline",
            "uptime_s": round(time.time() - self._start_ts, 1),
        }
