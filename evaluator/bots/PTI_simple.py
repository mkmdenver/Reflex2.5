from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Dict, Any, Optional, Tuple, Set

import json
import redis.asyncio as aioredis

from common import logging as log
from common.bus import subscribe, unpack, publish_async, scoped_key

COMPONENT = "eval.simple_pattern_from_stream"

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

ORDER_CHANNEL = os.getenv("EVAL_ORDER_INTENT_CHANNEL", "eval.order_intent")

SIMPLE_OFFSET = float(os.getenv("SIMPLE_OFFSET", "0.10"))
SIMPLE_MIN_CHANGE = float(os.getenv("SIMPLE_MIN_CHANGE", "0.01"))
SIMPLE_QTY = int(os.getenv("SIMPLE_QTY", "1"))
SIMPLE_ACCOUNT_TAG = os.getenv("SIMPLE_ACCOUNT_TAG", "SIM")
SIMPLE_SYMBOL = os.getenv("SIMPLE_SYMBOL", "SPY").upper()
SIMPLE_EXTENDED_HOURS = os.getenv("SIMPLE_EXTENDED_HOURS", "true").lower() == "true"

FILTER_STREAM_CHANNEL = os.getenv("EVAL_SIMPLE_FILTER_CHANNEL", "eval.simple_filter_stream")
BARS_CHANNEL = os.getenv("SIMPLE_BARS_CHANNEL", "hub.bars1m")

# Telemetry / EvalView
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
EVAL_STATE_CHANNEL = os.getenv("EVAL_STATE_CHANNEL", "eval.state")
EVAL_STATE_INTERVAL = float(os.getenv("EVAL_STATE_INTERVAL", "5.0"))

# Durable membership set maintained by FTS_simple2 (Option C)
# IMPORTANT: must match FTS_simple2's key naming (and be scoped!)
ACTIVE_SET_KEY_RAW = os.getenv("EVAL_SIMPLE2_ACTIVE_KEY", "eval:fts_simple2:active")
ACTIVE_SET_KEY = scoped_key(ACTIVE_SET_KEY_RAW)

# Supervisor behavior
RESTART_DELAY_SEC = float(os.getenv("EVAL_PTI_RESTART_DELAY_SEC", "1.0"))


# -----------------------------------------------------------------------------
# Simple state machine for "minute up" pattern
# -----------------------------------------------------------------------------

@dataclass
class SimpleMinuteUpState:
    symbol: str
    prev_close: Optional[float] = None
    this_close: Optional[float] = None
    last_status: str = "idle"
    last_diff: float = 0.0
    last_threshold: float = SIMPLE_OFFSET
    last_fire_ts: float = 0.0

    def update_from_close(self, close: float) -> Tuple[bool, Dict[str, Any]]:
        now = time.time()

        if self.this_close is not None:
            self.prev_close = self.this_close
        self.this_close = close

        if self.prev_close is None:
            self.last_status = "arming"
            self.last_diff = 0.0
            self.last_threshold = max(SIMPLE_OFFSET, SIMPLE_MIN_CHANGE)
            diag = {
                "symbol": self.symbol,
                "status": self.last_status,
                "diff": None,
                "threshold": self.last_threshold,
                "prev_close": None,
                "this_close": self.this_close,
                "last_fire_ts": self.last_fire_ts,
            }
            return False, diag

        diff = self.this_close - self.prev_close
        threshold = max(SIMPLE_OFFSET, SIMPLE_MIN_CHANGE)
        self.last_diff = diff
        self.last_threshold = threshold

        fired = diff >= threshold
        near = diff >= 0.75 * threshold

        if fired:
            status = "fired"
            self.last_fire_ts = now
        elif near:
            status = "near"
        else:
            status = "arming"

        self.last_status = status

        diag: Dict[str, Any] = {
            "symbol": self.symbol,
            "status": status,
            "diff": diff,
            "threshold": threshold,
            "prev_close": self.prev_close,
            "this_close": self.this_close,
            "last_fire_ts": self.last_fire_ts,
        }
        return fired, diag


class EvalTelemetry:
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
            log.exception(COMPONENT, "telemetry.publish_error", extra={"error": repr(exc)})


async def _bootstrap_active_set() -> Set[str]:
    try:
        redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        members = await redis.smembers(ACTIVE_SET_KEY)
        active = {m.upper() for m in members}
        log.info(COMPONENT, "active_set.bootstrap", extra={"active_symbols": sorted(active), "active_set_key": ACTIVE_SET_KEY})
        return active
    except Exception as exc:
        log.exception(COMPONENT, "active_set.bootstrap_error", extra={"error": repr(exc), "active_set_key": ACTIVE_SET_KEY})
        return set()


async def filter_loop(active: Set[str], stop_event: asyncio.Event) -> None:
    try:
        ps = await subscribe(FILTER_STREAM_CHANNEL)
        log.info(COMPONENT, "filter_loop.subscribe_ok", extra={"channel": FILTER_STREAM_CHANNEL})
    except Exception as exc:
        log.exception(COMPONENT, "filter_loop.subscribe_error", extra={"channel": FILTER_STREAM_CHANNEL, "error": repr(exc)})
        return

    try:
        async for msg in ps.listen():  # type: ignore[attr-defined]
            if stop_event.is_set():
                break
            if msg.get("type") != "message":
                continue
            raw = msg.get("data")
            if not raw:
                continue
            try:
                payload = unpack(raw)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue

            kind = payload.get("kind")
            sym = payload.get("symbol")
            if not sym:
                continue
            sym = str(sym).upper()

            if kind == "add":
                active.add(sym)
                log.info(COMPONENT, "filter_loop.add", extra={"symbol": sym, "active_count": len(active)})
            elif kind == "remove":
                active.discard(sym)
                log.info(COMPONENT, "filter_loop.remove", extra={"symbol": sym, "active_count": len(active)})

    except asyncio.CancelledError:
        log.info(COMPONENT, "filter_loop.cancelled")
        raise
    except Exception as exc:
        log.exception(COMPONENT, "filter_loop.error", extra={"error": repr(exc)})
    finally:
        log.info(COMPONENT, "filter_loop.stop")


async def bars_loop(active: Set[str], states: Dict[str, SimpleMinuteUpState], stop_event: asyncio.Event) -> None:
    telemetry = EvalTelemetry("PTI_simple1", ["simple_minute_up"])
    bars_seen = 0
    fires = 0

    try:
        ps = await subscribe(BARS_CHANNEL)
        log.info(COMPONENT, "bars_loop.subscribe_ok", extra={"channel": BARS_CHANNEL})
    except Exception as exc:
        log.exception(COMPONENT, "bars_loop.subscribe_error", extra={"channel": BARS_CHANNEL, "error": repr(exc)})
        return

    try:
        async for msg in ps.listen():  # type: ignore[attr-defined]
            if stop_event.is_set():
                break
            if msg.get("type") != "message":
                continue
            raw = msg.get("data")
            if not raw:
                continue

            try:
                payload = unpack(raw)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue

            bar = payload
            sym = (bar.get("sym") or bar.get("symbol") or "").upper()
            if not sym:
                continue

            if sym != SIMPLE_SYMBOL or sym not in active:
                continue

            bars_seen += 1

            raw_c = bar.get("c") or bar.get("close")
            try:
                close_val = float(raw_c) if raw_c is not None else None
            except Exception:
                close_val = None
            if close_val is None:
                continue

            st = states.get(sym)
            if st is None:
                st = SimpleMinuteUpState(symbol=sym)
                states[sym] = st

            fired, diag = st.update_from_close(close_val)

            log.info(COMPONENT, "pattern.diag", extra={"symbol": sym, "status": diag.get("status"), "diff": diag.get("diff"), "threshold": diag.get("threshold"), "bars_seen": bars_seen, "fired": fired})

            if fired:
                fires += 1
                intent = {
                    "symbol": sym,
                    "side": "buy",
                    "qty": SIMPLE_QTY,
                    "type": "market",
                    "time_in_force": "day",
                    "account_tag": SIMPLE_ACCOUNT_TAG,
                    "extended_hours": SIMPLE_EXTENDED_HOURS,
                    "note": f"simple_minute_up diff={diag['diff']:.4f} >= {diag['threshold']:.4f}",
                }
                envelope = {"intent": intent, "meta": {"model": "simple_minute_up", "strength": 1.0, "risk": 0.1, "diag": diag}}
                await publish_async(ORDER_CHANNEL, envelope)
                log.info(COMPONENT, "pattern.fire_long", extra={"symbol": sym, "fires": fires})

            await telemetry.maybe_publish({"active_symbols": len(active), "bars_seen": bars_seen, "fires": fires, "last_status": diag.get("status")})

    except asyncio.CancelledError:
        log.info(COMPONENT, "bars_loop.cancelled")
        raise
    except Exception as exc:
        log.exception(COMPONENT, "bars_loop.error", extra={"error": repr(exc)})
    finally:
        log.info(COMPONENT, "bars_loop.stop")


async def _run_forever() -> None:
    log.info(COMPONENT, "config", extra={
        "symbol": SIMPLE_SYMBOL,
        "filter_stream_channel": FILTER_STREAM_CHANNEL,
        "bars_channel": BARS_CHANNEL,
        "order_channel": ORDER_CHANNEL,
        "active_set_key": ACTIVE_SET_KEY,
        "restart_delay_sec": RESTART_DELAY_SEC,
    })

    active: Set[str] = await _bootstrap_active_set()
    states: Dict[str, SimpleMinuteUpState] = {}
    stop_event = asyncio.Event()

    def start_tasks() -> Tuple[asyncio.Task, asyncio.Task]:
        ft = asyncio.create_task(filter_loop(active, stop_event), name="pti.filter_loop")
        bt = asyncio.create_task(bars_loop(active, states, stop_event), name="pti.bars_loop")
        return ft, bt

    filter_task, bars_task = start_tasks()

    try:
        while not stop_event.is_set():
            done, _ = await asyncio.wait({filter_task, bars_task}, timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                continue

            for t in done:
                name = t.get_name()
                exc = None
                try:
                    exc = t.exception()
                except asyncio.CancelledError:
                    exc = None

                if exc is not None:
                    log.exception(COMPONENT, "task.crashed", extra={"task": name, "error": repr(exc)})
                else:
                    log.error(COMPONENT, "task.ended_unexpectedly", extra={"task": name})

            for t in (filter_task, bars_task):
                if not t.done():
                    t.cancel()

            await asyncio.sleep(RESTART_DELAY_SEC)
            filter_task, bars_task = start_tasks()

    finally:
        stop_event.set()
        for t in (filter_task, bars_task):
            if t and not t.done():
                t.cancel()
        log.info(COMPONENT, "shutdown.complete")


async def run() -> None:
    await _run_forever()


if __name__ == "__main__":
    asyncio.run(run())
