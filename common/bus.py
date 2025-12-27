import asyncio
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import msgpack
import redis.asyncio as redis

# Back-compat re-export (exists in your repo)
from common.ipc_bus import Bus, new_trace_id, ts_utc_ns, t_mono_ns  # type: ignore[import]


def _env(name: str, default: str) -> str:
    v = os.getenv(name)
    return v.strip() if v and v.strip() else default


# Prefer explicit REDIS_URL if set, otherwise use GARNET_URL
REDIS_URL = _env("REDIS_URL", _env("GARNET_URL", "redis://127.0.0.1:6379/0"))

REFLEX_INSTANCE_ID = _env("REFLEX_INSTANCE_ID", "default")
REFLEX_MODE = _env("REFLEX_MODE", "LIVE")  # LIVE | REPLAY
REFLEX_RUN_ID = _env("REFLEX_RUN_ID", "run0")  # set once per stack launch


def scoped_key(base: str) -> str:
    """
    Run-scoped key/channel/stream name to prevent stale artifacts between restarts.

      reflex:{instance}:{mode}:{run_id}:{base}
    """
    base = (base or "").strip(":")
    return f"reflex:{REFLEX_INSTANCE_ID}:{REFLEX_MODE}:{REFLEX_RUN_ID}:{base}"


# Tier command channel (instance-scoped) is configured via env
_tiers_cmd_q = os.getenv("TIERS_CMD_Q", "")
_raise_chan = (_tiers_cmd_q.replace("{instance_id}", REFLEX_INSTANCE_ID) if _tiers_cmd_q else "eval.raisetier")

CHANNELS = {
    "ticks": "hub.ticks",
    "quotes": "hub.quotes",
    "raise": _raise_chan,
    "order": "eval.order_intent",
    "admin_flags": "admin.flags",
    "backfill": "hub.backfill",
}


def pack(d: Dict[str, Any]) -> bytes:
    return msgpack.packb(d, use_bin_type=True)


def unpack(b: bytes) -> Dict[str, Any]:
    return msgpack.unpackb(b, raw=False)


async def publisher():
    return await redis.from_url(REDIS_URL, decode_responses=False)


async def publisher_text():
    return await redis.from_url(REDIS_URL, decode_responses=True)


# --------------------------------------------------------------------------------------
# Pub/Sub
# --------------------------------------------------------------------------------------

async def subscribe(ch_name: str):
    r = await redis.from_url(REDIS_URL, decode_responses=False)
    ps = r.pubsub()
    await ps.subscribe(ch_name)
    return ps


async def publish_async(channel: str, payload: Dict[str, Any]) -> None:
    """
    Async publish helper used by asyncio components (DataHub worker, evaluator bots).
    """
    r = await publisher()
    await r.publish(channel, pack(payload))


def publish_sync(channel: str, payload: Dict[str, Any]) -> None:
    """
    Sync publish helper used by WSGI/threaded entrypoints.
    """
    async def _inner():
        r = await publisher()
        await r.publish(channel, pack(payload))

    try:
        loop = asyncio.get_running_loop()
        asyncio.run_coroutine_threadsafe(_inner(), loop)
    except RuntimeError:
        asyncio.run(_inner())


# --------------------------------------------------------------------------------------
# Redis Streams (durable) - used for membership/intents etc.
# --------------------------------------------------------------------------------------

async def xadd_async(stream: str, payload: Dict[str, Any], maxlen: int = 20000) -> str:
    r = await publisher()
    fields = {"b": pack(payload)}
    return await r.xadd(stream, fields, maxlen=maxlen, approximate=True)


async def ensure_group(stream: str, group: str) -> None:
    r = await publisher_text()
    try:
        await r.xgroup_create(stream, group, id="0-0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def xreadgroup_blocking(
    stream: str,
    group: str,
    consumer: str,
    count: int = 100,
    block_ms: int = 5000,
):
    await ensure_group(stream, group)
    r = await publisher_text()
    return await r.xreadgroup(group, consumer, streams={stream: ">"}, count=count, block=block_ms)


async def xack(stream: str, group: str, *ids: str) -> int:
    r = await publisher_text()
    return await r.xack(stream, group, *ids)


def unpack_stream_entry(fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    b = fields.get("b")
    if b is None:
        return None
    if isinstance(b, str):
        b = b.encode("latin1")
    if not isinstance(b, (bytes, bytearray)):
        return None
    try:
        return unpack(bytes(b))
    except Exception:
        return None


@dataclass
class InternalEvent:
    kind: str
    symbol: str
    ts: float
    payload: Dict[str, Any]
