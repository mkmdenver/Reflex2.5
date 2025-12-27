# common/ipc_bus.py
from __future__ import annotations

import os, json, time, logging, socket
from typing import Any, Optional
from urllib.parse import urlparse

import redis.asyncio as redis  # pip install redis>=5

def _default_url() -> str:
    return (
        os.environ.get("REFLEX__GARNET_URL")
        or os.environ.get("GARNET_URL")
        or "redis://127.0.0.1:6379/0"
    )

def ts_utc_ns() -> int:
    return time.time_ns()

def t_mono_ns() -> int:
    return time.monotonic_ns()

def new_trace_id() -> str:
    return f"{ts_utc_ns()}-{t_mono_ns()}"

class Bus:
    """
    Minimal, Garnet/Redis-friendly IPC bus.
    - JSON queue & pub/sub
    - Soft concurrency gate (INCR/DECR)
    - Optional streams (best effort)
    - Simple cooperative locks (SET NX PX)
    """

    def __init__(
        self,
        url: Optional[str] = None,
        list_intents: str = "reflex:intents.list",
        stream_intents: str = "reflex:intents",
        key_inflight: str = "reflex:capacity:orders_inflight",
        key_max: str = "reflex:capacity:orders_max",
        stream_maxlen: int = 100_000,
    ) -> None:
        self.url = (url or _default_url()).rstrip("/")
        parsed = urlparse(self.url)
        db = int(parsed.path[1:] or "0") if parsed.path else 0

        self.r = redis.from_url(self.url, decode_responses=False, db=db)

        self.LIST_INTENTS = list_intents
        self.STREAM_INTENTS = stream_intents
        self.KEY_INFLIGHT = key_inflight
        self.KEY_MAX = key_max
        self.STREAM_MAXLEN = stream_maxlen

        self.logger = logging.getLogger("common.ipc_bus")
        if not self.logger.handlers:
            logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

        self.logger.debug(
            "Bus init url=%s list=%s stream=%s inflight=%s max=%s maxlen=%d default_max=%s",
            self.url, self.LIST_INTENTS, self.STREAM_INTENTS,
            self.KEY_INFLIGHT, self.KEY_MAX, self.STREAM_MAXLEN,
            os.environ.get("REFLEX__ORDERS_MAX_DEFAULT", "8"),
        )

    # ---- back-compat alias some callers use ----
    @property
    def log(self):
        return self.logger

    # ---------- JSON helpers ----------
    @staticmethod
    def _dumps(obj: Any) -> bytes:
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    async def lpush_json(self, key: str, obj: Any) -> int:
        return await self.r.lpush(key, self._dumps(obj))

    list_push_json = lpush_json  # alias

    async def publish(self, channel: str, obj: Any) -> int:
        return await self.r.publish(channel, self._dumps(obj))

    # ---------- Concurrency gate ----------
    async def gate_emit_intent(self, max_fallback: int = 8) -> bool:
        max_bytes = await self.r.get(self.KEY_MAX)
        try:
            max_allowed = int(max_bytes.decode() if isinstance(max_bytes, (bytes, bytearray)) else max_bytes)
        except Exception:
            max_allowed = int(max_fallback)

        inflight = await self.r.incr(self.KEY_INFLIGHT)
        if inflight > max_allowed:
            await self.r.decr(self.KEY_INFLIGHT)
            return False
        return True

    async def dec_inflight_safe(self) -> None:
        try:
            v = await self.r.decr(self.KEY_INFLIGHT)
            if v < 0:
                await self.r.set(self.KEY_INFLIGHT, 0)
                self.logger.warning("inflight dipped below zero; clamped to 0")
        except Exception as e:
            self.logger.error("dec_inflight_safe failed: %s", e)

    # ---------- Cooperative locks (no Lua) ----------
    def _default_owner(self) -> str:
        return f"{socket.gethostname()}:{os.getpid()}:{new_trace_id()}"

    async def try_acquire(self, key: str, ttl_ms: int = 10_000, owner: Optional[str] = None) -> Optional[str]:
        """
        Try to acquire a lock with SET NX PX. Returns owner token if acquired, else None.
        Note: no automatic renewal—call renew_lock() from a watchdog if you need it.
        """
        token = owner or self._default_owner()
        # Redis-py: set(name, value, nx=True, px=ttl_ms)
        ok = await self.r.set(key, token.encode("utf-8"), nx=True, px=ttl_ms)
        if ok:
            return token
        return None

    async def renew_lock(self, key: str, owner: str, ttl_ms: int = 10_000) -> bool:
        """
        Renew TTL only if we still own the lock. Best effort without Lua:
        read-compare-pexpire; acceptable for cooperative use.
        """
        cur = await self.r.get(key)
        if cur == (owner.encode("utf-8") if isinstance(owner, str) else owner):
            # PEXPIRE returns 1 on success
            try:
                res = await self.r.pexpire(key, ttl_ms)
                return bool(res)
            except Exception:
                return False
        return False

    async def release_lock(self, key: str, owner: str) -> bool:
        """
        Release lock if owned by 'owner'. Best effort compare-and-delete (no Lua).
        """
        cur = await self.r.get(key)
        if cur == (owner.encode("utf-8") if isinstance(owner, str) else owner):
            try:
                await self.r.delete(key)
                return True
            except Exception:
                return False
        return False

    # ---------- Optional streams (best-effort) ----------
    async def xadd(self, stream: str, obj: Any, maxlen: Optional[int] = None) -> Optional[str]:
        try:
            return await self.r.xadd(
                stream, {"m": self._dumps(obj)}, maxlen=maxlen or self.STREAM_MAXLEN, approximate=True
            )
        except Exception as e:
            self.logger.debug("XADD to %s failed (non-fatal): %s", stream, getattr(e, "args", e))
            return None
