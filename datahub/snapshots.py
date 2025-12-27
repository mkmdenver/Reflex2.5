import threading
from typing import Dict, Any, Optional


class Snapshots:
    """
    Very small in-memory snapshot helper for DataHub API and tools.

    This is intentionally dumb:
    - It keeps only the "last seen" trade / quote / 1m bar per symbol.
    - It is safe to read from multiple threads with a simple lock.
    - It does NOT try to be a time-series database; that's Timescale's job.

    Engine / cockpit panels can read from here to show quick gauges.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.by_symbol: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Updaters
    # ------------------------------------------------------------------
    def _get_snap(self, sym: str) -> Dict[str, Any]:
        sym = sym.upper()
        snap = self.by_symbol.get(sym)
        if snap is None:
            snap = {}
            self.by_symbol[sym] = snap
        return snap

    def on_trade(self, msg: Dict[str, Any]) -> None:
        sym = (msg.get("symbol") or "").upper()
        if not sym:
            return
        with self._lock:
            snap = self._get_snap(sym)
            snap["last_trade"] = msg

    def on_quote(self, msg: Dict[str, Any]) -> None:
        sym = (msg.get("symbol") or "").upper()
        if not sym:
            return
        with self._lock:
            snap = self._get_snap(sym)
            snap["last_quote"] = msg

    def on_bar_1m(self, msg: Dict[str, Any]) -> None:
        """
        Optional hook for 1-minute bars. If DataHub publishes bars into
        Redis and also calls this, cockpit panels and any future snapshot
        APIs can answer "what is the last bar we saw for XYZ?" without
        waking up Postgres.
        """
        sym = (msg.get("sym") or msg.get("symbol") or "").upper()
        if not sym:
            return
        with self._lock:
            snap = self._get_snap(sym)
            snap["last_bar_1m"] = msg

    # ------------------------------------------------------------------
    # Readers
    # ------------------------------------------------------------------
    def get(self, sym: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self.by_symbol.get(sym.upper())

    def all(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            # shallow copy; inner dicts are shared on purpose
            return dict(self.by_symbol)
