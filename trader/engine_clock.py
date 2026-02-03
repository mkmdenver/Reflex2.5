# trader/engine_clock.py
"""Engine clock for LIVE vs REPLAY.

Goal:
  - In LIVE, "now" is wall-clock.
  - In REPLAY, "now" is driven by replay timestamps (ticks/bars/clock pulses).

This module is intentionally tiny and dependency-free so it can be imported
anywhere (TradeRunner, MD worker, etc.) without creating cycles.
"""

from __future__ import annotations

import os
import time
from typing import Optional

_ENGINE_NOW_TS: Optional[float] = None

def is_replay_mode() -> bool:
    return (os.getenv("REFLEX_MODE") or os.getenv("MODE") or "LIVE").strip().upper() == "REPLAY"

def set_engine_now(ts: float) -> None:
    """Set the engine clock (seconds since epoch). Monotonic non-decreasing."""
    global _ENGINE_NOW_TS
    try:
        tsf = float(ts)
    except Exception:
        return
    if tsf <= 0:
        return
    if _ENGINE_NOW_TS is None or tsf >= _ENGINE_NOW_TS:
        _ENGINE_NOW_TS = tsf

def get_engine_now() -> Optional[float]:
    return _ENGINE_NOW_TS

def now() -> float:
    """Return the correct 'now' for gating/time-based logic."""
    if is_replay_mode():
        if _ENGINE_NOW_TS is not None:
            return float(_ENGINE_NOW_TS)
        # If replay hasn't produced any timestamps yet, fall back (but caller should expect this early).
        return float(time.time())
    return float(time.time())
