import threading
from typing import Dict, Any, Optional

class Snapshots:
    def __init__(self):
        self._lock = threading.Lock()
        self.by_symbol: Dict[str, Dict[str, Any]] = {}

    def on_trade(self, msg: Dict[str, Any]):
        sym = (msg.get("symbol") or "").upper()
        if not sym: return
        with self._lock:
            snap = self.by_symbol.setdefault(sym, {})
            snap["last_trade"] = msg

    def on_quote(self, msg: Dict[str, Any]):
        sym = (msg.get("symbol") or "").upper()
        if not sym: return
        with self._lock:
            snap = self.by_symbol.setdefault(sym, {})
            snap["last_quote"] = msg

    def get(self, sym: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self.by_symbol.get(sym.upper())

    def all(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return dict(self.by_symbol)
