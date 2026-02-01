"""trader/intent_worker.py

Trader Intent Router (Redis -> Trader HTTP /v1/intents)

Why this exists:
- Evaluator/PTI bots publish intents on Redis (instance-scoped channel).
- Trader should have ONE execution front-door: HTTP /v1/intents.
- This worker forwards intents to that HTTP endpoint (local loopback), so the
  trading logic lives in Trader (TradeRunner / risk / telemetry), not in bots.

Safety:
- By default we DO NOT publish 'orders' onto broker channels.
- Legacy dispatch can be enabled explicitly for older stacks.

Env:
- BOT_INTENT_CHANNEL / PTI_INTENT_CHANNEL / INTENT_CHANNEL / ORDER_CHANNEL:
    channel name (supports {instance_id}); default eval.intent.<instance>
- TRADER_HTTP_BASE: default http://127.0.0.1:{TRADER_API_PORT}
- ENABLE_INTENT_HTTP_FORWARD: default 1 (true)
- ENABLE_LEGACY_DISPATCH: default 0
"""

from __future__ import annotations

import asyncio
import os
from typing import Dict, Any, Optional

import httpx

from common.bus import subscribe, unpack, publish_async
from common import logging as log

COMPONENT = "trader.intent_router"

REFLEX_INSTANCE_ID = (os.getenv("REFLEX_INSTANCE_ID") or "default").strip()

def _resolve_channel(default_base: str) -> str:
    ch = (
        os.getenv("BOT_INTENT_CHANNEL")
        or os.getenv("PTI_INTENT_CHANNEL")
        or os.getenv("INTENT_CHANNEL")
        or os.getenv("ORDER_CHANNEL")
        or default_base
    ).strip()

    if "{instance_id}" in ch:
        ch = ch.format(instance_id=REFLEX_INSTANCE_ID)

    # enforce scoping for bare bases
    if ch in (default_base, "eval.intent", "eval.order_intent"):
        ch = f"{ch}.{REFLEX_INSTANCE_ID}"

    return ch

INTENT_CHANNEL = _resolve_channel("eval.intent")

ENABLE_INTENT_HTTP_FORWARD = (os.getenv("ENABLE_INTENT_HTTP_FORWARD") or "1").strip().lower() in ("1","true","yes","on")
ENABLE_LEGACY_DISPATCH = (os.getenv("ENABLE_LEGACY_DISPATCH") or "").strip().lower() in ("1", "true", "yes", "on")

SIM_CHANNEL = (os.getenv("SIM_ORDER_CHANNEL") or "trader.sim.orders").strip()
ALPACA_CH = (os.getenv("ALPACA_ORDER_CHANNEL") or "trader.alpaca.orders").strip()

TRADER_API_PORT = int((os.getenv("TRADER_API_PORT") or "7002").strip() or 7002)
TRADER_HTTP_BASE = (os.getenv("TRADER_HTTP_BASE") or os.getenv("TRADER_API_BASE") or f"http://127.0.0.1:{TRADER_API_PORT}").strip().rstrip("/")

def _safe_decode(x):
    if isinstance(x, (bytes, bytearray, memoryview)):
        try:
            return bytes(x).decode("utf-8", "replace")
        except Exception:
            return f"<bytes len={len(x)}>"
    return x

async def _dispatch_legacy(intent: Dict[str, Any]):
    acct = (intent.get("account_id") or "SIM").lower()

    if acct in ("sim", "demo"):
        await publish_async(SIM_CHANNEL, intent)
        log.info(COMPONENT, "dispatch.sim", extra={"channel": SIM_CHANNEL, "symbol": intent.get("symbol"), "side": intent.get("side")})
    elif acct.startswith("alpaca"):
        await publish_async(ALPACA_CH, intent)
        log.info(COMPONENT, "dispatch.alpaca", extra={"channel": ALPACA_CH, "account_id": intent.get("account_id"), "symbol": intent.get("symbol"), "side": intent.get("side")})
    else:
        log.error(COMPONENT, "dispatch.unknown_account", extra={"account_id": intent.get("account_id"), "symbol": intent.get("symbol"), "side": intent.get("side")})

async def _forward_http(intent: Dict[str, Any], meta: Dict[str, Any]) -> None:
    url = f"{TRADER_HTTP_BASE}/v1/intents"
    payload = {"intent": intent, "meta": meta}
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.post(url, json=payload)
        if r.status_code >= 400:
            log.error(COMPONENT, "forward.http_failed", extra={"status": r.status_code, "body": (r.text or "")[:200]})
        else:
            try:
                j = r.json()
            except Exception:
                j = {}
            log.info(COMPONENT, "forward.http_ok", extra={"status": r.status_code, "trade_id": j.get("trade_id"), "symbol": intent.get("symbol"), "side": intent.get("side"), "account_id": intent.get("account_id")})
    except Exception as e:
        log.error(COMPONENT, "forward.http_exception", extra={"error": str(e), "url": url})

async def run():
    log.info(COMPONENT, "startup", extra={"sub": INTENT_CHANNEL, "http_forward": ENABLE_INTENT_HTTP_FORWARD, "legacy_dispatch": ENABLE_LEGACY_DISPATCH, "trader_http": TRADER_HTTP_BASE})
    ps = await subscribe(INTENT_CHANNEL)

    async for msg in ps.listen():  # redis pubsub
        if msg.get("type") != "message":
            continue

        ch = _safe_decode(msg.get("channel"))
        data = msg.get("data")
        data_len = len(data) if isinstance(data, (bytes, bytearray, memoryview)) else None
        log.info(COMPONENT, "received.intent", extra={"channel": ch, "data_len": data_len})

        try:
            envelope = unpack(msg["data"])
            intent = envelope.get("intent")
            meta = envelope.get("meta", {})
        except Exception:
            log.exception(COMPONENT, "bad_intent_payload", extra={"channel": ch, "data_len": data_len})
            continue

        if not intent:
            log.warning(COMPONENT, "no_intent_in_msg", extra={"envelope_keys": list(envelope.keys())})
            continue

        log.info(
            COMPONENT,
            "intent.observed",
            extra={
                "intent_id": intent.get("intent_id"),
                "symbol": intent.get("symbol"),
                "side": intent.get("side"),
                "strategy_id": intent.get("strategy_id"),
                "source": intent.get("source"),
                "strength": intent.get("strength"),
                "account_id": intent.get("account_id"),
            },
        )

        if ENABLE_INTENT_HTTP_FORWARD:
            await _forward_http(intent, meta if isinstance(meta, dict) else {})
        elif ENABLE_LEGACY_DISPATCH:
            await _dispatch_legacy({**intent, "meta": meta})

if __name__ == "__main__":
    asyncio.run(run())
