from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Dict, Any, Optional, Set

import json
import redis.asyncio as aioredis

from common import logging as log
from common.bus import subscribe, unpack, publish_async, CHANNELS

COMPONENT = "eval.simple_filter_to_stream"

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

SIMPLE_SYMBOL = os.getenv("SIMPLE_SYMBOL", "SPY").upper()
SIMPLE_MIN_DAY_VOL = int(os.getenv("SIMPLE_MIN_DAY_VOL", "100000"))
SIMPLE_BOOTSTRAP_TIER = os.getenv("SIMPLE_BOOTSTRAP_TIER", "HOT").upper()

FILTER_STREAM_CHANNEL = os.getenv(
    "EVAL_SIMPLE_FILTER_CHANNEL", "eval.simple_filter_stream"
)
BARS_CHANNEL = os.getenv("SIMPLE_BARS_CHANNEL", "hub.bars1m")

# Telemetry / EvalView
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
EVAL_STATE_CHANNEL = os.getenv("EVAL_STATE_CHANNEL", "eval.state")
EVAL_STATE_INTERVAL = float(os.getenv("EVAL_STATE_INTERVAL", "5.0"))

# Canonical active-set mirror for PTIs to bootstrap from
ACTIVE_SET_KEY = os.getenv("EVAL_SIMPLE_ACTIVE_KEY", "eval:fts_simple1:active")


# -----------------------------------------------------------------------------
# Data structures
# -----------------------------------------------------------------------------

@dataclass
class SymbolFilterState:
    symbol: str
    day_vol: int = 0
    last_price: Optional[float] = None
    active: bool = False
    last_reason: str = "init"


class EvalTelemetry:
    """
    Helper to publish eval state snapshots that EvalView consumes.
    """

    def __init__(self, eval_id: str, modules: list[str]) -> None:
        self.eval_id = eval_id
        self.modules = modules
        self.rows = 1
        self._redis: Optional[aioredis.Redis] = None
        self._last_publish: float = 0.0

    async def maybe_publish(self, stats: Dict[str, Any]) -> None:
        now = time.time()
        if now - self._last_publish < EVAL_STATE_INTERVAL:
            return
        self._last_publish = now

        if self._redis is None:
            self._redis = aioredis.from_url(REDIS_URL, decode_responses=True)

        payload = {
            "eval_id": self.eval_id,
            "modules": self.modules,
            "rows": self.rows,
            "last_update": now,
            "stats": stats,
        }
        try:
            await self._redis.publish(EVAL_STATE_CHANNEL, json.dumps(payload))
        except Exception as exc:
            # Telemetry should never kill the loop
            log.exception(
                COMPONENT,
                "telemetry.publish_error",
                extra={"error": repr(exc)},
            )


# -----------------------------------------------------------------------------
# Tier bootstrap
# -----------------------------------------------------------------------------

async def _bootstrap_tier(symbol: str) -> None:
    """
    Request a tier raise for `symbol` via CHANNELS["raise"] so DataHub's
    TierListener will subscribe the live adapter and kick off ticks/bars.
    """
    tier = SIMPLE_BOOTSTRAP_TIER
    payload: Dict[str, Any] = {
        "symbol": symbol,
        "tier": tier,
        "source": COMPONENT,
    }
    await publish_async(CHANNELS["raise"], payload)
    log.info(
        COMPONENT,
        "bootstrap_tier.requested",
        extra={"symbol": symbol, "tier": tier, "channel": CHANNELS["raise"]},
    )


# -----------------------------------------------------------------------------
# Main loop
# -----------------------------------------------------------------------------

async def run() -> None:
    """
    Main loop: bootstrap tier, subscribe to 1m bars, maintain per-symbol
    volume, and emit add/remove events when the rule transitions.
    Also periodically publishes EvalView telemetry and mirrors the
    active symbol set into a Redis set so pattern modules (PTI) can
    bootstrap their own active lists even if they start later.
    """
    log.info(
        COMPONENT,
        "startup",
        extra={
            "symbol": SIMPLE_SYMBOL,
            "min_day_vol": SIMPLE_MIN_DAY_VOL,
            "bars_channel": BARS_CHANNEL,
            "filter_stream_channel": FILTER_STREAM_CHANNEL,
            "bootstrap_tier": SIMPLE_BOOTSTRAP_TIER,
            "active_set_key": ACTIVE_SET_KEY,
        },
    )

    # Ask DataHub to raise the tier for our simple symbol so ticks/bars flow.
    await _bootstrap_tier(SIMPLE_SYMBOL)

    state = SymbolFilterState(symbol=SIMPLE_SYMBOL)

    # Local view of active symbols (for telemetry) and optional Redis mirror.
    active_symbols: Set[str] = set()
    redis_active: Optional[aioredis.Redis] = None

    bars_seen = 0
    adds_emitted = 0
    removes_emitted = 0

    telemetry = EvalTelemetry("FTS_simple1", ["filter_to_stream"])

    try:
        ps = await subscribe(BARS_CHANNEL)
        log.info(
            COMPONENT,
            "bars.subscribe_ok",
            extra={"channel": BARS_CHANNEL},
        )
    except Exception as exc:
        log.exception(
            COMPONENT,
            "bars.subscribe_error",
            extra={"channel": BARS_CHANNEL, "error": repr(exc)},
        )
        return

    try:
        async for msg in ps.listen():  # type: ignore[attr-defined]
            if msg.get("type") != "message":
                continue

            try:
                bar = unpack(msg["data"])
            except Exception as exc:
                log.exception(
                    COMPONENT,
                    "bars.unpack_error",
                    extra={"error": repr(exc)},
                )
                continue

            sym = bar.get("sym") or bar.get("symbol")
            if not sym or str(sym).upper() != SIMPLE_SYMBOL:
                continue

            bars_seen += 1

            v = bar.get("v") or bar.get("volume") or 0
            c = bar.get("c") or bar.get("close")

            try:
                v = int(v)
            except Exception:
                v = 0

            close_val: Optional[float] = None
            if c is not None:
                try:
                    close_val = float(c)
                except Exception:
                    close_val = None

            state.day_vol += v
            state.last_price = close_val

            passed = state.day_vol >= SIMPLE_MIN_DAY_VOL
            prev_active = state.active
            state.active = passed
            state.last_reason = "passed" if passed else "volume_below_min"

            # Lazily create Redis client for mirroring active set.
            if redis_active is None:
                try:
                    redis_active = aioredis.from_url(
                        REDIS_URL,
                        decode_responses=True,
                    )
                    log.info(
                        COMPONENT,
                        "active_set.redis_init_ok",
                        extra={"redis_url": REDIS_URL, "active_set_key": ACTIVE_SET_KEY},
                    )
                except Exception as exc:
                    log.exception(
                        COMPONENT,
                        "active_set.redis_init_error",
                        extra={"error": repr(exc)},
                    )
                    redis_active = None

            if passed and not prev_active:
                evt = {
                    "kind": "add",
                    "symbol": state.symbol,
                    "ts": bar.get("t_recv_ns"),
                    "source": COMPONENT,
                    "payload": {
                        "reason": state.last_reason,
                        "day_vol": state.day_vol,
                        "price": state.last_price,
                    },
                }
                await publish_async(FILTER_STREAM_CHANNEL, evt)
                active_symbols.add(state.symbol)
                adds_emitted += 1

                # Mirror into Redis so PTI processes can bootstrap their
                # active set even if they start after this event.
                if redis_active is not None:
                    try:
                        await redis_active.sadd(ACTIVE_SET_KEY, state.symbol)
                        log.info(
                            COMPONENT,
                            "active_set.sadd_ok",
                            extra={
                                "key": ACTIVE_SET_KEY,
                                "symbol": state.symbol,
                                "active_count": len(active_symbols),
                            },
                        )
                    except Exception as exc:
                        log.exception(
                            COMPONENT,
                            "active_set.sadd_error",
                            extra={"error": repr(exc)},
                        )

                log.info(
                    COMPONENT,
                    "filter.add",
                    extra=evt["payload"] | {"symbol": state.symbol},
                )

            elif (not passed) and prev_active:
                evt = {
                    "kind": "remove",
                    "symbol": state.symbol,
                    "ts": bar.get("t_recv_ns"),
                    "source": COMPONENT,
                    "payload": {
                        "reason": state.last_reason,
                        "day_vol": state.day_vol,
                        "price": state.last_price,
                    },
                }
                await publish_async(FILTER_STREAM_CHANNEL, evt)
                active_symbols.discard(state.symbol)
                removes_emitted += 1

                if redis_active is not None:
                    try:
                        await redis_active.srem(ACTIVE_SET_KEY, state.symbol)
                        log.info(
                            COMPONENT,
                            "active_set.srem_ok",
                            extra={
                                "key": ACTIVE_SET_KEY,
                                "symbol": state.symbol,
                                "active_count": len(active_symbols),
                            },
                        )
                    except Exception as exc:
                        log.exception(
                            COMPONENT,
                            "active_set.srem_error",
                            extra={"error": repr(exc)},
                        )

                log.info(
                    COMPONENT,
                    "filter.remove",
                    extra=evt["payload"] | {"symbol": state.symbol},
                )
            else:
                # No state change; only log at debug to avoid noise.
                log.debug(
                    COMPONENT,
                    "filter.no_change",
                    extra={
                        "symbol": state.symbol,
                        "active": state.active,
                        "day_vol": state.day_vol,
                        "price": state.last_price,
                    },
                )

            # Telemetry heartbeat
            await telemetry.maybe_publish(
                {
                    "symbol": state.symbol,
                    "day_vol": state.day_vol,
                    "last_price": state.last_price,
                    "active": state.active,
                    "bars_seen": bars_seen,
                    "adds_emitted": adds_emitted,
                    "removes_emitted": removes_emitted,
                    "active_count": len(active_symbols),
                }
            )

    except asyncio.CancelledError:
        log.info(COMPONENT, "shutdown.cancelled")
    except Exception as exc:
        log.exception(COMPONENT, "shutdown.error", extra={"error": repr(exc)})
    finally:
        log.info(COMPONENT, "shutdown.complete")


if __name__ == "__main__":
    asyncio.run(run())
