# trader/events.py
from __future__ import annotations
import time
from typing import Any, Dict, Iterable

def now_ts() -> float:
    """Wall-clock seconds since epoch (float)."""
    return time.time()

def now_ts_ns() -> int:
    """Nanoseconds since epoch (int) for stable ordering in logs/streams."""
    try:
        return time.time_ns()
    except AttributeError:
        return int(time.time() * 1_000_000_000)

def make_event(kind: str, **fields: Any) -> Dict[str, Any]:
    """
    Minimal, schema-lite event envelope.
    Callers (e.g., Alerts.emit) can add instance/account/etc. via **fields.
    """
    ev = {"ts": now_ts(), "ts_ns": now_ts_ns(), "event": kind}
    ev.update(fields)
    return ev

# Keep this list in sync with cockpit filters and any alert routing logic.
EVENTS: tuple[str, ...] = (
    "RECONCILE_OK", "RECONCILE_FAIL",
    "INTENT_ENQUEUED", "INTENT_ACK",
    "RISK_REJECT", "GUARD_REJECT",
    "ROUTED", "BROKER_ACK", "FILL", "PARTIAL_FILL",
    "CANCELLED", "EXPIRED", "ERROR",
    "AUTO_ENABLED", "AUTO_DISABLED",
    "HUMAN_OVERRIDE",
    "CANCEL_ALL_REQ", "FLATTEN_REQ",
    # Execution “weather”
    "SYMBOL_DEGRADED", "BROKER_DEGRADED",
    # Policy breadcrumbs
    "ADD_BLOCKED", "EXIT_POLICY",
)

EVENTS_SET = frozenset(EVENTS)

def is_valid_event(kind: str) -> bool:
    """Optional helper if a caller wants to assert membership."""
    return kind in EVENTS_SET

def all_events() -> Iterable[str]:
    """Expose the canonical list for UIs/tests without importing the module constant."""
    return EVENTS
