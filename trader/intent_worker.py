"""
Trader Intent Router — consumes eval.order_intent and sends to broker adapters.
Supports: SIM + Alpaca Paper
"""

from __future__ import annotations
import asyncio, json, os
from typing import Dict, Any
from common.bus import subscribe, unpack
from common import logging as log

COMPONENT = "trader.intent_router"

ORDER_CHANNEL = os.getenv("ORDER_CHANNEL", "eval.order_intent")
SIM_CHANNEL  = os.getenv("SIM_ORDER_CHANNEL", "trader.sim.orders")
ALPACA_CH    = os.getenv("ALPACA_ORDER_CHANNEL", "trader.alpaca.orders")

async def dispatch(intent: Dict[str, Any]):
    acct = (intent.get("account_id") or "SIM").upper()

    if acct in ("SIM","DEMO"):
        await publish_async(SIM_CHANNEL, intent)
        log.info(COMPONENT, "dispatch.sim",  extra=int)

    elif acct in ("PAPER","ALPACA","APAPER"):
        await publish_async(ALPACA_CH, intent)
        log.info(COMPONENT, "dispatch.paper", extra=int)

    else:
        log.error(COMPONENT,"dispatch.unknown_acct",extra={"account":acct,"intent":intent})

async def run():
    log.info(COMPONENT,"startup",extra={"sub":ORDER_CHANNEL})

    ps = await subscribe(ORDER_CHANNEL)

    async for msg in ps.listen():  # redis stream
        if msg.get("type")!="message": continue

        try:
            envelope = unpack(msg["data"])
            intent = envelope.get("intent")
            meta   = envelope.get("meta",{})
        except:
            log.exception(COMPONENT,"bad_intent_payload",extra={"raw":msg})
            continue

        if not intent:
            log.warn(COMPONENT,"no_intent_in_msg",extra={"msg":envelope})
            continue

        await dispatch({**intent,"meta":meta})

if __name__=="__main__":
    asyncio.run(run())
