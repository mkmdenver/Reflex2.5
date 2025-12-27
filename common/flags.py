# common/flags.py
from __future__ import annotations

class FlagCache:
    """Process-wide cache of symbol flags (e.g., DO_NOT_TRADE)."""
    def __init__(self):
        self._m: dict[str, dict[str, object]] = {}
    def set(self, sym: str, key: str, val: object) -> None:
        self._m.setdefault(sym, {})[key] = val
    def get(self, sym: str, key: str, default=None):
        return self._m.get(sym, {}).get(key, default)
    def snapshot(self) -> dict[str, dict[str, object]]:
        return {k: v.copy() for k, v in self._m.items()}

FLAGS = FlagCache()

def is_tradable(flags: dict | None) -> bool:
    if not flags:
        return True  # default to tradable if flags missing
    return not (
        flags.get("no_trade", False)
        or flags.get("halted", False)
        or flags.get("restricted", False)
    )