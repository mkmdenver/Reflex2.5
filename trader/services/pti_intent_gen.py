# trader/pti_intent_gen.py
# Version: 2025-12-17
# Purpose:
#   Testing helper: publish PTI-like "order intent" envelopes into the same Redis
#   channel that the trader/broker_worker consumes (CHANNELS["order"]).
#
# Why:
#   Lets you test the full Trader pipeline (intent -> /v1/orders -> Alpaca paper)
#   without needing the evaluator/PTI stack running.
#
# Usage examples:
#   python -m trader.pti_intent_gen --account-id paper1 --symbol SPY --side buy --notional 50
#   python -m trader.pti_intent_gen --account-id paper1 --symbols SPY,AAPL --loop --seconds 30
#
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from typing import Any, Dict, Optional, List

try:
    import redis.asyncio as redis  # type: ignore
except Exception:  # pragma: no cover
    redis = None  # type: ignore

from common.bus import CHANNELS
from .core import get_logger, get_instance_id, get_redis_url

log = get_logger("trader.pti_intent_gen")


def _now() -> float:
    return time.time()


def _truthy(v: Optional[str]) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "y", "on")


def _build_intent(args: argparse.Namespace, symbol: str) -> Dict[str, Any]:
    # Keep shape compatible with broker_worker expectations.
    # NOTE: qty vs notional: pick one.
    intent: Dict[str, Any] = {
        "intent_id": f"pti_gen:{get_instance_id()}:{int(_now()*1000)}:{random.randint(1000,9999)}",
        "account_id": args.account_id,
        "symbol": symbol.upper(),
        "side": args.side.lower(),
        "order_type": args.order_type,
        "time_in_force": args.tif,
        "extended_hours": bool(args.extended_hours),
        "note": args.note or "pti_intent_gen",
    }

    if args.qty is not None:
        intent["qty"] = int(args.qty)
    if args.notional is not None:
        intent["notional"] = float(args.notional)

    # Optional pricing hints
    if args.limit_price is not None:
        intent["limit_price"] = float(args.limit_price)
    if args.stop_price is not None:
        intent["stop_price"] = float(args.stop_price)
    if args.trail is not None:
        intent["trail"] = float(args.trail)

    return intent


def _envelope(intent: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    # PTI-ish envelope: intent + meta
    return {
        "intent": intent,
        "meta": {
            "model": args.model,
            "bot": args.bot,
            "strength": float(args.strength),
            "ts": _now(),
            "source": "pti_intent_gen",
        },
    }


async def _publish_once(r: Any, channel: str, msg: Dict[str, Any]) -> None:
    raw = json.dumps(msg, separators=(",", ":"), default=str)
    await r.publish(channel, raw)


async def run(argv: Optional[List[str]] = None) -> int:
    if redis is None:
        raise RuntimeError("redis package not available (install redis>=4).")

    ap = argparse.ArgumentParser(prog="pti_intent_gen", description="Publish PTI-like intents for Trader testing.")
    ap.add_argument("--account-id", required=True, help="Broker account_id (must exist in Trader broker DB).")
    ap.add_argument("--symbols", default="SPY", help="Comma-separated symbols. Default: SPY")
    ap.add_argument("--symbol", default=None, help="Single symbol (overrides --symbols if provided).")

    ap.add_argument("--side", choices=["buy", "sell"], default="buy")
    ap.add_argument("--order-type", default="market", choices=["market", "limit", "stop", "stop_limit"])
    ap.add_argument("--tif", default="day", help="Time in force (day, gtc, etc).")
    ap.add_argument("--qty", type=int, default=None, help="Share quantity (mutually exclusive with --notional).")
    ap.add_argument("--notional", type=float, default=None, help="Notional USD amount (mutually exclusive with --qty).")

    ap.add_argument("--limit-price", type=float, default=None)
    ap.add_argument("--stop-price", type=float, default=None)
    ap.add_argument("--trail", type=float, default=None)

    ap.add_argument("--extended-hours", action="store_true", help="Set extended_hours=True in intent.")
    ap.add_argument("--note", default=None)

    ap.add_argument("--model", default="pti.mock")
    ap.add_argument("--bot", default="pti_mock_gen")
    ap.add_argument("--strength", type=float, default=0.85)

    ap.add_argument("--loop", action="store_true", help="Publish repeatedly.")
    ap.add_argument("--seconds", type=int, default=30, help="Loop interval seconds.")
    ap.add_argument("--jitter", type=int, default=5, help="Random +/- jitter seconds when looping.")

    args = ap.parse_args(argv)

    if args.qty is not None and args.notional is not None:
        raise SystemExit("Choose only one: --qty or --notional")

    symbols = []
    if args.symbol:
        symbols = [args.symbol.strip()]
    else:
        symbols = [s.strip() for s in (args.symbols or "").split(",") if s.strip()]
    if not symbols:
        raise SystemExit("No symbols provided.")

    channel = CHANNELS.get("order", "eval.order_intent")
    url = get_redis_url()
    r = redis.from_url(url, decode_responses=True)

    log.info("pti_intent_gen.start", extra={"channel": channel, "symbols": symbols, "redis": url})

    async def publish_batch() -> None:
        for sym in symbols:
            intent = _build_intent(args, sym)
            env = _envelope(intent, args)
            await _publish_once(r, channel, env)
            log.info("pti_intent_gen.published", extra={"symbol": sym, "intent_id": intent.get("intent_id")})

    if not args.loop:
        await publish_batch()
        return 0

    while True:
        await publish_batch()
        base = max(1, int(args.seconds))
        jit = int(args.jitter)
        sleep_s = base + random.randint(-jit, jit) if jit > 0 else base
        await asyncio.sleep(max(1, sleep_s))


def _main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        log.info("pti_intent_gen.keyboard_interrupt")
        return 0
    except Exception as exc:
        log.exception("pti_intent_gen.crashed", extra={"error": repr(exc)})
        return 1


if __name__ == "__main__" or __package__ == "trader.pti_intent_gen":
    sys.exit(_main())
