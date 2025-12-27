from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, Set, List

import json
import aiohttp
import redis.asyncio as aioredis

from common import logging as log
from common.bus import publish_async, CHANNELS, scoped_key

COMPONENT = "eval.simple2_filter_to_stream"
DATAHUB_API_BASE = os.getenv("DATAHUB_API_BASE", "http://localhost:7000")

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

# Stage 1 universe (fundamental filter placeholder)
# Example: SIMPLE2_SYMBOLS="SPY,TSLA,NVDA"
SIMPLE2_SYMBOLS = os.getenv("SIMPLE2_SYMBOLS", "SPY")

# Filter stream channel (ephemeral hints only; PTI should NOT depend on this)
FILTER_STREAM_CHANNEL = os.getenv("EVAL_SIMPLE_FILTER_CHANNEL", "eval.simple_filter_stream")

# DataHub tier control:
SIMPLE2_BOOTSTRAP_TIER = os.getenv("SIMPLE2_BOOTSTRAP_TIER", "WATCH")
SIMPLE2_ACTIVE_TIER = os.getenv("SIMPLE2_ACTIVE_TIER", "WARM")

# Stage 2 scan interval (seconds) – 30-second loop
STAGE2_INTERVAL_SEC = float(os.getenv("SIMPLE2_INTERVAL_SEC", "30.0"))

# How many bars of history to keep per symbol in Stage 2
STAGE2_HISTORY_BARS = int(os.getenv("SIMPLE2_HISTORY_BARS", "60"))

# Minimal bars / volume thresholds for the Stage 2 decision.
STAGE2_MIN_BARS = int(os.getenv("SIMPLE2_MIN_BARS", "5"))
SIMPLE2_MIN_AVG_VOL = int(os.getenv("SIMPLE2_MIN_AVG_VOL", "10000"))

# Telemetry / EvalView
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
EVAL_STATE_CHANNEL = os.getenv("EVAL_STATE_CHANNEL", "eval.state")
EVAL_STATE_INTERVAL = float(os.getenv("EVAL_STATE_INTERVAL", "5.0"))

# Durable membership key (Option C) – scoped per run
ACTIVE_SET_KEY_RAW = os.getenv("EVAL_SIMPLE2_ACTIVE_KEY", "eval:fts_simple2:active")
ACTIVE_SET_KEY = scoped_key(ACTIVE_SET_KEY_RAW)


# -----------------------------------------------------------------------------
# Dataclasses for Stage 1 and Stage 2
# -----------------------------------------------------------------------------

@dataclass
class Stage1Record:
    symbol: str
    fundamentals: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Stage2SymbolState:
    symbol: str
    bars: List[Dict[str, Any]] = field(default_factory=list)
    last_ts: Optional[int] = None
    last_metrics: Dict[str, Any] = field(default_factory=dict)


class EvalTelemetry:
    def __init__(self, eval_id: str, modules: List[str]) -> None:
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
            log.exception(
                COMPONENT,
                "telemetry.publish_error",
                extra={"error": repr(exc)},
            )


# -----------------------------------------------------------------------------
# Stage 1: build base universe
# -----------------------------------------------------------------------------

def _build_stage1_universe() -> Dict[str, Stage1Record]:
    raw = SIMPLE2_SYMBOLS
    syms: Set[str] = set()

    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        syms.add(part.upper())

    universe: Dict[str, Stage1Record] = {}
    for sym in sorted(syms):
        universe[sym] = Stage1Record(symbol=sym, fundamentals={})

    log.info(
        COMPONENT,
        "stage1.universe_built",
        extra={"symbols": sorted(universe.keys())},
    )
    return universe


async def _raise_tier(symbol: str, tier: str) -> None:
    """
    Promote/demote a symbol's DataHub tier via the bus channel.
    """
    try:
        evt = {
            "symbol": symbol,
            "tier": tier,
            "ts": time.time(),
            "source": COMPONENT,
        }
        await publish_async(CHANNELS["raise"], evt)

        log.info(
            COMPONENT,
            "stage2.raise_tier.requested",
            extra={"symbol": symbol, "tier": tier, "channel": CHANNELS["raise"]},
        )
    except Exception as exc:
        log.exception(
            COMPONENT,
            "stage2.raise_tier.error",
            extra={"symbol": symbol, "tier": tier, "error": repr(exc)},
        )


# -----------------------------------------------------------------------------
# DataHub pull (prime-the-pump)
# -----------------------------------------------------------------------------

async def _fetch_initial_history(symbol: str, limit: int) -> List[Dict[str, Any]]:
    url = f"{DATAHUB_API_BASE}/v1/history/bars1m"
    params = {"symbol": symbol, "limit": limit}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=5.0) as resp:
                data = await resp.json()
    except Exception as exc:
        log.exception(
            COMPONENT,
            "datahub.fetch_initial_history.error",
            extra={"symbol": symbol, "error": repr(exc)},
        )
        return []

    if not isinstance(data, dict) or not data.get("ok"):
        log.warn(
            COMPONENT,
            "datahub.fetch_initial_history.not_ok",
            extra={"symbol": symbol, "raw": data},
        )
        return []

    bars = data.get("bars") or []
    if not isinstance(bars, list):
        return []
    return bars


async def _init_stage2_states(
    universe: Dict[str, Stage1Record],
    states: Dict[str, Stage2SymbolState],
) -> None:
    for sym in universe.keys():
        st = Stage2SymbolState(symbol=sym)
        states[sym] = st
        bars = await _fetch_initial_history(sym, STAGE2_HISTORY_BARS)
        if bars:
            if len(bars) > STAGE2_HISTORY_BARS:
                bars = bars[-STAGE2_HISTORY_BARS:]
            st.bars = bars
            st.last_ts = bars[-1].get("ts")
        log.info(
            COMPONENT,
            "stage2.state_initialized",
            extra={"symbol": sym, "bars_seeded": len(st.bars)},
        )

    log.info(
        COMPONENT,
        "stage2.states_initialized",
        extra={"count": len(states)},
    )


# -----------------------------------------------------------------------------
# Stage 2 indicators + decision logic
# -----------------------------------------------------------------------------

def _compute_indicators(state: Stage2SymbolState) -> Dict[str, Any]:
    bars = state.bars
    n = len(bars)

    if n == 0:
        return {"symbol": state.symbol, "active": False, "reason": "no_bars"}

    closes = [b["close"] for b in bars]
    vols = [b["volume"] for b in bars]

    total_vol = sum(vols)
    avg_vol = total_vol / max(1, n)
    last_close = closes[-1]
    prev_close = closes[-2] if n >= 2 else None
    price_change = last_close - prev_close if prev_close is not None else 0.0

    if n < STAGE2_MIN_BARS:
        return {
            "symbol": state.symbol,
            "active": False,
            "reason": "insufficient_bars",
            "bars": n,
            "avg_vol": avg_vol,
            "last_close": last_close,
            "price_change": price_change,
        }

    if avg_vol < SIMPLE2_MIN_AVG_VOL:
        return {
            "symbol": state.symbol,
            "active": False,
            "reason": "avg_vol_too_low",
            "bars": n,
            "avg_vol": avg_vol,
            "last_close": last_close,
            "price_change": price_change,
        }

    return {
        "symbol": state.symbol,
        "active": True,
        "reason": "volume_ok",
        "bars": n,
        "avg_vol": avg_vol,
        "last_close": last_close,
        "price_change": price_change,
    }


# -----------------------------------------------------------------------------
# Stage 2 main loop
# -----------------------------------------------------------------------------

async def stage2_loop(
    universe: Dict[str, Stage1Record],
    states: Dict[str, Stage2SymbolState],
    stop_event: asyncio.Event,
) -> None:
    telemetry = EvalTelemetry("FTS_simple2", ["stage1_filter", "stage2_dynamic"])

    active_symbols: Set[str] = set()
    redis_active: Optional[aioredis.Redis] = None

    adds_emitted = 0
    removes_emitted = 0
    total_scans = 0

    universe_syms = sorted(universe.keys())

    while not stop_event.is_set():
        loop_start = time.time()
        total_scans += 1

        # Lazy Redis init for durable membership set
        if redis_active is None:
            try:
                redis_active = aioredis.from_url(REDIS_URL, decode_responses=True)
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

        warm_candidates = 0
        symbols_evaluated = 0

        for sym in universe_syms:
            st = states.get(sym)
            if st is None:
                st = Stage2SymbolState(symbol=sym)
                states[sym] = st

            fresh_bars = await _fetch_initial_history(sym, STAGE2_HISTORY_BARS)
            if fresh_bars:
                if len(fresh_bars) > STAGE2_HISTORY_BARS:
                    fresh_bars = fresh_bars[-STAGE2_HISTORY_BARS:]
                st.bars = fresh_bars
                st.last_ts = st.bars[-1].get("ts")

            metrics = _compute_indicators(st)
            st.last_metrics = metrics

            should_be_active = bool(metrics.get("active", False))
            reason = metrics.get("reason", "unknown")
            symbols_evaluated += 1

            if should_be_active:
                warm_candidates += 1
                if sym not in active_symbols:
                    active_symbols.add(sym)

                    # Durable membership add
                    if redis_active is not None:
                        try:
                            await redis_active.sadd(ACTIVE_SET_KEY, sym)
                            log.info(
                                COMPONENT,
                                "active_set.sadd_ok",
                                extra={
                                    "key": ACTIVE_SET_KEY,
                                    "symbol": sym,
                                    "active_count": len(active_symbols),
                                },
                            )
                        except Exception as exc:
                            log.exception(
                                COMPONENT,
                                "active_set.sadd_error",
                                extra={"symbol": sym, "error": repr(exc)},
                            )

                    # Optional ephemeral hint
                    evt = {
                        "kind": "add",
                        "symbol": sym,
                        "ts": time.time(),
                        "source": COMPONENT,
                        "payload": {"reason": reason, "metrics": metrics},
                    }
                    try:
                        await publish_async(FILTER_STREAM_CHANNEL, evt)
                    except Exception as exc:
                        log.exception(COMPONENT, "filter_stream.publish_error", extra={"error": repr(exc)})

                    await _raise_tier(sym, SIMPLE2_ACTIVE_TIER)
                    adds_emitted += 1

            else:
                if sym in active_symbols:
                    active_symbols.remove(sym)

                    # Durable membership remove
                    if redis_active is not None:
                        try:
                            await redis_active.srem(ACTIVE_SET_KEY, sym)
                            log.info(
                                COMPONENT,
                                "active_set.srem_ok",
                                extra={
                                    "key": ACTIVE_SET_KEY,
                                    "symbol": sym,
                                    "active_count": len(active_symbols),
                                },
                            )
                        except Exception as exc:
                            log.exception(
                                COMPONENT,
                                "active_set.srem_error",
                                extra={"symbol": sym, "error": repr(exc)},
                            )

                    # Optional ephemeral hint
                    evt = {
                        "kind": "remove",
                        "symbol": sym,
                        "ts": time.time(),
                        "source": COMPONENT,
                        "payload": {"reason": reason, "metrics": metrics},
                    }
                    try:
                        await publish_async(FILTER_STREAM_CHANNEL, evt)
                    except Exception as exc:
                        log.exception(COMPONENT, "filter_stream.publish_error", extra={"error": repr(exc)})

                    await _raise_tier(sym, SIMPLE2_BOOTSTRAP_TIER)
                    removes_emitted += 1

        loop_end = time.time()
        elapsed = loop_end - loop_start

        stats = {
            "universe_count": len(universe_syms),
            "symbols_evaluated": symbols_evaluated,
            "warm_candidates": warm_candidates,
            "active_count": len(active_symbols),
            "adds_emitted": adds_emitted,
            "removes_emitted": removes_emitted,
            "interval_sec": STAGE2_INTERVAL_SEC,
            "elapsed_sec": elapsed,
            "total_scans": total_scans,
            "active_set_key": ACTIVE_SET_KEY,
        }
        log.info(COMPONENT, "stage2.heartbeat", extra=stats)
        await telemetry.maybe_publish(stats)

        sleep_for = max(0.0, STAGE2_INTERVAL_SEC - elapsed)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=sleep_for)
        except asyncio.TimeoutError:
            pass

    log.info(COMPONENT, "stage2.loop_exit")


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

async def run() -> None:
    log.info(
        COMPONENT,
        "startup",
        extra={
            "symbols": [s.strip() for s in SIMPLE2_SYMBOLS.split(",") if s.strip()],
            "filter_stream_channel": FILTER_STREAM_CHANNEL,
            "bootstrap_tier": SIMPLE2_BOOTSTRAP_TIER,
            "active_tier": SIMPLE2_ACTIVE_TIER,
            "interval_sec": STAGE2_INTERVAL_SEC,
            "history_bars": STAGE2_HISTORY_BARS,
            "min_bars": STAGE2_MIN_BARS,
            "min_avg_vol": SIMPLE2_MIN_AVG_VOL,
            "active_set_key": ACTIVE_SET_KEY,
        },
    )

    universe = _build_stage1_universe()

    # Bootstrap symbols into WATCH (or whatever you set)
    for sym in universe.keys():
        await _raise_tier(sym, SIMPLE2_BOOTSTRAP_TIER)

    states: Dict[str, Stage2SymbolState] = {}
    await _init_stage2_states(universe, states)

    stop_event = asyncio.Event()
    stage2_task = asyncio.create_task(stage2_loop(universe, states, stop_event))

    try:
        await asyncio.wait({stage2_task}, return_when=asyncio.FIRST_EXCEPTION)
    finally:
        stop_event.set()
        if not stage2_task.done():
            stage2_task.cancel()
        log.info(COMPONENT, "shutdown.complete")


if __name__ == "__main__":
    asyncio.run(run())
