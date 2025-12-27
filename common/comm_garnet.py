# common/comm_garnet.py
from __future__ import annotations
import os
import json
import redis
from typing import Any, Optional

# One shared client per process
_CLIENT: Optional[redis.Redis] = None

def rcli() -> redis.Redis:
    """Return a cached Redis/Garnet client.

    Uses env GARNET_URL, e.g. redis://127.0.0.1:6379/0
    We keep decode_responses=False so PubSub payloads are bytes and we control decoding.
    """
    global _CLIENT
    if _CLIENT is None:
        url = os.getenv("GARNET_URL", "redis://127.0.0.1:6379/0")
        _CLIENT = redis.Redis.from_url(
            url,
            decode_responses=False,          # bytes in, bytes out (fast; you decode explicitly)
            socket_timeout=5,
            socket_connect_timeout=5,
            health_check_interval=30,
        )
    return _CLIENT

# Back-compat alias (if any code still imports this name)
def garnet_rcli() -> redis.Redis:
    return rcli()

def publish(channel: str, payload: Any) -> int:
    """Publish JSON (or str/bytes) to channel. Returns number of subscribers that received it."""
    r = rcli()
    if not isinstance(payload, (bytes, str)):
        payload = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return r.publish(channel, payload)

__all__ = ["rcli", "garnet_rcli", "publish"]
