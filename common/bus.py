# common/bus.py
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import msgpack

# Async Redis (used by DataHub, etc.)
import redis.asyncio as redis_async

# Sync Redis (used by WSGI / waitress paths and any sync tools)
import redis as redis_sync_mod

# Back-compat re-export (exists in your repo)
from common.ipc_bus import Bus, new_trace_id, ts_utc_ns, t_mono_ns  # type: ignore[import]


def _env(name: str, default: str) -> str:
    v = os.getenv(name)
    return v.strip() if v and v.strip() else default


def _fmt_instance(s: str) -> str:
    """
    Expand "{instance_id}" placeholders using REFLEX_INSTANCE_ID.

    If REFLEX_INSTANCE_ID is missing/empty, we leave the string unchanged
    (better to surface misconfig than silently publish to the wrong channel).
    """
    inst = (os.getenv("REFLEX_INSTANCE_ID") or "").strip()
    if inst and "{instance_id}" in s:
        return s.replace("{instance_id}", inst)
    return s


def _env_fmt(name: str, default: str) -> str:
    return _fmt_instance(_env(name, default))


# Allow older code to override raise channel in env.
# IMPORTANT: format instance placeholder so TIERS_CMD_Q=reflex:{instance_id}:cmd.tiers works.
_raise_chan = _env_fmt("REFLEX_RAISE_TIER_CHANNEL", _env("TIERS_CMD_Q", "eval.raisetier"))

# Canonical DataHub feed channel envs (your .env uses these)
_TICKS_LIVE = _env_fmt("REFLEX_DATAHUB_TICKS_PUB_LIVE", "hub.ticks.pub.live")
_QUOTES_LIVE = _env_fmt("REFLEX_DATAHUB_QUOTES_PUB_LIVE", "hub.quotes.pub.live")
_BARS1M_LIVE = _env_fmt("REFLEX_DATAHUB_BARS1M_PUB_LIVE", "hub.bars1m.pub.live")
_SNAP_LIVE = _env_fmt("REFLEX_DATAHUB_SNAP_PUB_LIVE", "hub.snap.pub.live")

_TICKS_REPLAY = _env_fmt("REFLEX_DATAHUB_TICKS_PUB_REPLAY", "hub.ticks.pub.replay")
_QUOTES_REPLAY = _env_fmt("REFLEX_DATAHUB_QUOTES_PUB_REPLAY", "hub.quotes.pub.replay")
_BARS1M_REPLAY = _env_fmt("REFLEX_DATAHUB_BARS1M_PUB_REPLAY", "hub.bars1m.pub.replay")
_SNAP_REPLAY = _env_fmt("REFLEX_DATAHUB_SNAP_PUB_REPLAY", "hub.snap.pub.replay")

# Optional generic aliases (older env names)
_BARS1M_ALIAS = _env_fmt("REFLEX_BARS1M_PUB", _env("REFLEX_BARS1M_PUB_LIVE", _BARS1M_LIVE))


CHANNELS: Dict[str, str] = {
    # Tier / control plane
    "raise": _raise_chan,
    "order": _env_fmt("REFLEX_ORDER_INTENT_CHANNEL", "eval.order_intent"),
    "admin_flags": _env_fmt("REFLEX_ADMIN_FLAGS_CHANNEL", "admin.flags"),
    "backfill": _env_fmt("REFLEX_BACKFILL_CHANNEL", "hub.backfill"),

    # LIVE + REPLAY feeds (explicit)
    "ticks_live": _TICKS_LIVE,
    "quotes_live": _QUOTES_LIVE,
    "bars1m_live": _BARS1M_LIVE,
    "snap_live": _SNAP_LIVE,

    "ticks_replay": _TICKS_REPLAY,
    "quotes_replay": _QUOTES_REPLAY,
    "bars1m_replay": _BARS1M_REPLAY,
    "snap_replay": _SNAP_REPLAY,

    # Back-compat aliases (MOST CODE USES THESE)
    # Default them to LIVE to match your current architecture.
    "ticks": _env_fmt("REFLEX_DATAHUB_TICKS_PUB", _TICKS_LIVE),
    "quotes": _env_fmt("REFLEX_DATAHUB_QUOTES_PUB", _QUOTES_LIVE),
    "bars1m": _BARS1M_ALIAS,
    "snap": _env_fmt("REFLEX_DATAHUB_SNAP_PUB", _SNAP_LIVE),
}


@dataclass
class PubSub:
    pub: Any
    sub: Any


# ----------------------------
# Packing helpers
# ----------------------------

def pack(obj: Any) -> bytes:
    return msgpack.packb(obj, use_bin_type=True)


def unpack(buf: Any) -> Any:
    if buf is None:
        return None
    if isinstance(buf, (bytes, bytearray)):
        return msgpack.unpackb(buf, raw=False)
    return buf


# ----------------------------
# Redis clients (async + sync)
# ----------------------------

_async_redis: Optional[redis_async.Redis] = None
_async_pub: Optional[redis_async.Redis] = None

_sync_redis: Optional[redis_sync_mod.Redis] = None
_sync_pub: Optional[redis_sync_mod.Redis] = None


def redis_url() -> str:
    # Garnet + Redis compatibility
    return _env("GARNET_URL", _env("REDIS_URL", "redis://127.0.0.1:6379/0"))


async def get_redis() -> redis_async.Redis:
    global _async_redis
    if _async_redis is None:
        _async_redis = redis_async.from_url(redis_url())
    return _async_redis


async def publisher() -> redis_async.Redis:
    global _async_pub
    if _async_pub is None:
        _async_pub = await get_redis()
    return _async_pub


async def subscriber() -> PubSub:
    r = await get_redis()
    return PubSub(pub=r, sub=r.pubsub())


def get_redis_sync() -> redis_sync_mod.Redis:
    global _sync_redis
    if _sync_redis is None:
        _sync_redis = redis_sync_mod.from_url(redis_url())
    return _sync_redis


def publisher_sync() -> redis_sync_mod.Redis:
    global _sync_pub
    if _sync_pub is None:
        _sync_pub = get_redis_sync()
    return _sync_pub


# ----------------------------
# Async publish/subscribe
# ----------------------------

async def publish_async(channel: str, obj: Any) -> int:
    r = await publisher()
    return await r.publish(channel, pack(obj))


async def subscribe(channel: str) -> Any:
    ps = (await subscriber()).sub
    await ps.subscribe(channel)
    return ps


# ----------------------------
# Sync publish/subscribe (REST/WSGI, tools, etc.)
# ----------------------------

def publish_sync(channel: str, obj: Any) -> int:
    """
    Synchronous publish. Required by parts of the codebase that run under
    waitress/WSGI or other sync contexts.
    """
    r = publisher_sync()
    return int(r.publish(channel, pack(obj)))


def subscribe_sync(channel: str) -> Any:
    """
    Synchronous subscribe (rarely used, but handy for tools).
    """
    r = get_redis_sync()
    ps = r.pubsub()
    ps.subscribe(channel)
    return ps


def blpop_sync(key: str, timeout: int = 0) -> Optional[Any]:
    """
    Sync BLPOP helper for queue-style channels (if any parts of the system use it).
    Returns unpacked payload or None.
    """
    r = get_redis_sync()
    res = r.blpop(key, timeout=timeout)
    if not res:
        return None
    _k, payload = res
    return unpack(payload)
