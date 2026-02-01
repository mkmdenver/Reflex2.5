# trader/md_worker.py
# Version: 2025-12-17
# Purpose:
#   Subscribe to DataHub market-data channels (ticks + quotes) and maintain a
#   lightweight in-memory market state for Trader execution logic.
#
# Why this exists:
#   • Broker state tells us what actually filled; market data tells us what we *should* do next.
#   • Starts with prints + NBBO only (bars are optional later).
#
# Notes:
#   • Uses common.bus subscribe/unpack/CHANNELS so we stay consistent with the rest of Reflex.
#   • Optionally mirrors latest state to Redis keys for observability / other processes.
#
# Env:
#   TRADER_MD_ENABLE_REDIS_CACHE=1|0   (default 1)
#   TRADER_MD_KEY_PREFIX=reflex:{instance_id}:md
#   TRADER_MD_LOG_EVERY_SECS=10
#
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional

try:
    import redis.asyncio as redis  # type: ignore
except Exception:  # pragma: no cover
    redis = None  # type: ignore

from common.bus import subscribe, unpack, CHANNELS
from .core import get_logger, get_instance_id, get_redis_url

log = get_logger("trader.md")


@dataclass
class LastTrade:
    ts: float
    price: float
    size: int
    exchange: Optional[int] = None


@dataclass
class LastQuote:
    ts: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[int] = None
    ask_size: Optional[int] = None


@dataclass
class MarketState:
    symbol: str
    last_trade: Optional[LastTrade] = None
    last_quote: Optional[LastQuote] = None
    updated_ts: float = 0.0


def _truthy(v: Optional[str]) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "y", "on")


def _key_prefix(instance_id: str) -> str:
    # Example: reflex:liveA:md
    tpl = os.getenv("TRADER_MD_KEY_PREFIX", "reflex:{instance_id}:md")
    return tpl.format(instance_id=instance_id).rstrip(":")


def _now() -> float:
    return time.time()


def _coerce_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _coerce_int(x: Any) -> Optional[int]:
    try:
        if x is None:
            return None
        return int(x)
    except Exception:
        return None


async def _maybe_write_state(r: Any, prefix: str, st: MarketState) -> None:
    # Write a single JSON blob per symbol (easy to inspect).
    # key = reflex:liveA:md:SPY
    if not r:
        return
    key = f"{prefix}:{st.symbol.upper()}"
    payload = asdict(st)
    # dataclasses -> nested dataclasses become dicts, but Optional dataclasses become dict/None.
    await r.set(key, json.dumps(payload, separators=(",", ":"), default=str), ex=60)


def _parse_tick(payload: Dict[str, Any]) -> Optional[LastTrade]:
    # Polygon trade payloads vary slightly depending on adapter, but generally:
    # {sym|symbol, p|price, s|size, t|ts, x|exchange}
    sym = payload.get("symbol") or payload.get("sym")
    if not sym:
        return None

    price = _coerce_float(payload.get("price") if "price" in payload else payload.get("p"))
    size = _coerce_int(payload.get("size") if "size" in payload else payload.get("s"))
    if price is None or size is None:
        return None

    # Use a best-effort timestamp: sip_timestamp/participant/ts or now.
    ts = _coerce_float(payload.get("ts")) or _coerce_float(payload.get("t")) or _now()
    exch = _coerce_int(payload.get("exchange") if "exchange" in payload else payload.get("x"))
    return LastTrade(ts=float(ts), price=float(price), size=int(size), exchange=exch)


def _parse_quote(payload: Dict[str, Any]) -> Optional[LastQuote]:
    # Polygon quote payloads generally:
    # bid: bp, bid_size: bs, ask: ap, ask_size: as, ts: t
    sym = payload.get("symbol") or payload.get("sym")
    if not sym:
        return None

    bid = _coerce_float(payload.get("bid") if "bid" in payload else payload.get("bp"))
    ask = _coerce_float(payload.get("ask") if "ask" in payload else payload.get("ap"))
    bid_size = _coerce_int(payload.get("bid_size") if "bid_size" in payload else payload.get("bs"))
    ask_size = _coerce_int(payload.get("ask_size") if "ask_size" in payload else payload.get("as"))

    ts = _coerce_float(payload.get("ts")) or _coerce_float(payload.get("t")) or _now()
    return LastQuote(ts=float(ts), bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size)


async def _listen_channel(
    channel: str,
    kind: str,
    states: Dict[str, MarketState],
    r: Any,
    key_prefix: str,
) -> None:
    ps = await subscribe(channel)
    log.info("md_worker.listen.start", extra={"channel": channel, "kind": kind})

    async for msg in ps.listen():  # type: ignore[attr-defined]
        if msg.get("type") != "message":
            continue

        raw = msg.get("data")
        try:
            env = unpack(raw)
        except Exception:
            # If it's not in the packed/enveloped format, try raw JSON.
            try:
                env = json.loads(raw) if isinstance(raw, (bytes, str)) else raw
            except Exception:
                continue

        payload = env.get("payload") if isinstance(env, dict) else None
        if not isinstance(payload, dict):
            # Some senders might put fields at top-level
            payload = env if isinstance(env, dict) else None
        if not isinstance(payload, dict):
            continue

        sym = (payload.get("symbol") or payload.get("sym") or "").upper()
        if not sym:
            continue

        st = states.get(sym)
        if not st:
            st = MarketState(symbol=sym)
            states[sym] = st

        updated = False
        if kind == "tick":
            t = _parse_tick(payload)
            if t:
                st.last_trade = t
                updated = True
        elif kind == "quote":
            q = _parse_quote(payload)
            if q:
                st.last_quote = q
                updated = True

        if updated:
            st.updated_ts = _now()
            if r:
                # Fire and forget; don't stall the hot path.
                asyncio.create_task(_maybe_write_state(r, key_prefix, st))


async def _report_loop(states: Dict[str, MarketState], every: int) -> None:
    while True:
        await asyncio.sleep(max(1, every))
        # Summarize a few symbols for visibility
        items = list(states.values())
        items.sort(key=lambda x: x.updated_ts, reverse=True)
        top = items[:5]
        summary = []
        for st in top:
            lt = st.last_trade.price if st.last_trade else None
            b = st.last_quote.bid if st.last_quote else None
            a = st.last_quote.ask if st.last_quote else None
            summary.append(f"{st.symbol} lt={lt} b={b} a={a}")
        log.info("md_worker.state", extra={"symbols": len(states), "top": "; ".join(summary)})


async def run() -> int:
    instance = get_instance_id()
    # DataHub (LIVE build) prefers *_live channels when present. If Trader
    # listens only to the legacy channels, it can go blind (no md keys).
    ticks_ch = CHANNELS.get("ticks_live", CHANNELS.get("ticks", "hub.ticks"))
    quotes_ch = CHANNELS.get("quotes_live", CHANNELS.get("quotes", "hub.quotes"))

    enable_cache = _truthy(os.getenv("TRADER_MD_ENABLE_REDIS_CACHE", "1"))
    r = None
    if enable_cache:
        if redis is None:
            log.warning("md_worker.redis_missing")
        else:
            r = redis.from_url(get_redis_url(), decode_responses=True)

    states: Dict[str, MarketState] = {}
    key_prefix = _key_prefix(instance)

    report_every = int(os.getenv("TRADER_MD_LOG_EVERY_SECS", "10"))

    tasks = [
        asyncio.create_task(_listen_channel(ticks_ch, "tick", states, r, key_prefix)),
        asyncio.create_task(_listen_channel(quotes_ch, "quote", states, r, key_prefix)),
        asyncio.create_task(_report_loop(states, report_every)),
    ]

    log.info(
        "md_worker.start",
        extra={
            "instance": instance,
            "ticks": ticks_ch,
            "quotes": quotes_ch,
            "redis_cache": bool(r),
            "key_prefix": key_prefix,
        },
    )

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    for t in pending:
        t.cancel()
    for t in done:
        exc = t.exception()
        if exc:
            raise exc
    return 0


def _main() -> int:
    try:
        asyncio.run(run())
        return 0
    except KeyboardInterrupt:
        log.info("md_worker.keyboard_interrupt")
        return 0
    except Exception as exc:
        log.exception("md_worker.crashed", extra={"error": repr(exc)})
        return 1


if __name__ == "__main__" or __package__ == "trader.md_worker":
    sys.exit(_main())
