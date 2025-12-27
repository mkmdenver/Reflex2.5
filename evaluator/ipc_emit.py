# evaluator/ipc_emit.py
"""
Order-intent emitter for Evaluator.

- Routes to a per-trader queue if client_tag is present:
    client_tag = "auto"  -> LPUSH reflex:intents:auto
  Otherwise falls back to the default:
    LPUSH reflex:intents.list

- Uses simple inflight/max gating with the shared keys:
    reflex:capacity:orders_inflight
    reflex:capacity:orders_max

Return: True if enqueued (gate open), False if gate closed.
"""

from __future__ import annotations
import os
import json
import time
from typing import Dict, Any

# Bus is a lightweight holder of a sync redis client at .r
from common.ipc_bus import Bus  # just for type hints/consistency, not required

KEY_INFLIGHT = os.getenv("REFLEX__KEY_INFLIGHT", "reflex:capacity:orders_inflight")
KEY_MAX      = os.getenv("REFLEX__KEY_MAX",       "reflex:capacity:orders_max")

DEFAULT_LIST = os.getenv("REFLEX__INTENTS_LIST",  "reflex:intents.list")
ROUTE_PREFIX = os.getenv("REFLEX__INTENTS_ROUTE_PREFIX", "reflex:intents:")

def _ts_utc_ns() -> int:
    return int(time.time_ns())

def _t_mono_ns() -> int:
    # On Windows/Python 3.12 monotonic_ns exists and is stable for tracing
    return time.monotonic_ns()

def _new_trace_id(ts_ns: int, mono_ns: int) -> str:
    # Stable, sortable enough for our use: "<utc-ns>-<mono-ns>"
    return f"{ts_ns}-{mono_ns}"

def _choose_queue(payload: Dict[str, Any]) -> str:
    """
    If payload['client_tag'] is set and non-empty, route to
    'reflex:intents:<client_tag>'. Otherwise use DEFAULT_LIST.
    """
    tag = (payload.get("client_tag") or "").strip()
    if tag:
        return f"{ROUTE_PREFIX}{tag}"
    return DEFAULT_LIST

def emit_order_intent(bus: Bus, payload: Dict[str, Any]) -> bool:
    """
    Gate on inflight/max. If allowed, LPUSH an envelope to the chosen queue.
    Envelope schema matches what Trader expects: trace_id, ts_utc_ns, t_mono_ns, topic, payload.
    """
    r = bus.r  # sync redis client

    # Read max (fallback 8), then increment inflight
    max_allowed = int(r.get(KEY_MAX) or 8)
    inflight = r.incr(KEY_INFLIGHT)

    if inflight > max_allowed:
        # Gate closed: revert inflight increment and return False
        r.decr(KEY_INFLIGHT)
        return False

    ts_ns   = _ts_utc_ns()
    mono_ns = _t_mono_ns()
    trace   = _new_trace_id(ts_ns, mono_ns)

    envelope = {
        "trace_id":   trace,
        "ts_utc_ns":  ts_ns,
        "t_mono_ns":  mono_ns,
        "topic":      "orders.intent",
        "payload":    payload,
    }

    qname = _choose_queue(payload)

    # Enqueue JSON (the Trader consumer takes strings and json.loads them)
    r.lpush(qname, json.dumps(envelope))

    # Successful enqueue — leave inflight incremented; Trader will decrement on terminal (ACK/FILL) handling
    return True
