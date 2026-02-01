from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, Optional

# Ensure repo root is on sys.path
ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common import logging as log  # type: ignore
from common.bus import subscribe, unpack, CHANNELS  # type: ignore

COMPONENT = "tools.tap_intents"

def _with_instance_suffix(channel: str, instance_id: str, reflex_mode: str) -> str:
    ch = (channel or "").strip()
    if not ch:
        return ch

    # allow explicit templating
    if "{instance_id}" in ch:
        return ch.replace("{instance_id}", instance_id)

    if (reflex_mode or "").upper() != "LIVE":
        return ch

    inst = (instance_id or "").strip()
    if not inst:
        return ch

    if ch.endswith(f".{inst}"):
        return ch

    if ch in ("eval.intent", "manual.intent"):
        return f"{ch}.{inst}"

    if ch.startswith("eval.intent.") or ch.startswith("manual.intent."):
        return ch

    return ch

def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return v.strip() if v and v.strip() else default


def _resolve_intent_channel() -> str:
    # Allow override (rare), otherwise use canonical bus channel
    ch = _env("REFLEX_INTENTS_PUB", _env("PTI_INTENT_CHANNEL", CHANNELS.get("order", "eval.order_intent")))
    return _with_instance_suffix(ch, _env("REFLEX_INSTANCE_ID", ""), _env("REFLEX_MODE", ""))


INTENT_TAP_VERBOSE = _env("INTENT_TAP_VERBOSE", "0").lower() in ("1", "true", "yes", "on")


async def run() -> None:
    """
    Tap the unified intent stream.

    - Subscribes to the canonical intent channel.
    - Logs a compact summary for each intent envelope.
    - Optional full envelope dump if INTENT_TAP_VERBOSE=1.
    """
    channel = _resolve_intent_channel()

    log.info(
        COMPONENT,
        "startup",
        extra={
            "root": ROOT,
            "reflex_mode": _env("REFLEX_MODE", "-"),
            "channel": channel,
            "verbose": INTENT_TAP_VERBOSE,
        },
    )

    msg_count = 0
    last_hb = time.time()

    ps = await subscribe(channel)
    log.info(COMPONENT, "subscribe_ok", extra={"channel": channel})

    async for msg in ps.listen():  # type: ignore[attr-defined]
        if msg.get("type") != "message":
            continue

        raw = msg.get("data")
        if not raw:
            continue

        try:
            payload = unpack(raw)
        except Exception:
            # fallback: some publishers may send raw JSON strings
            try:
                if isinstance(raw, (bytes, bytearray)):
                    payload = json.loads(raw.decode("utf-8", errors="ignore"))
                else:
                    payload = json.loads(raw)
            except Exception:
                log.warn(COMPONENT, "unpack_failed", extra={"raw_type": str(type(raw))})
                continue

        if not isinstance(payload, dict):
            log.warn(COMPONENT, "payload_not_dict", extra={"payload_type": str(type(payload))})
            continue

        msg_count += 1

        intent: Dict[str, Any] = payload.get("intent") or {}
        meta: Dict[str, Any] = payload.get("meta") or payload.get("diag") or {}

        symbol = (intent.get("symbol") or "").upper()
        side = (intent.get("side") or "").lower()
        strategy_id = intent.get("strategy_id") or meta.get("model") or payload.get("source_component")
        strength = intent.get("strength") or meta.get("strength")
        urgency = intent.get("urgency")
        account_id: Optional[str] = intent.get("account_id") or intent.get("account_tag")

        log.info(
            COMPONENT,
            "intent",
            extra={
                "msg_count": msg_count,
                "symbol": symbol,
                "side": side,
                "strategy_id": strategy_id,
                "strength": strength,
                "urgency": urgency,
                "account": account_id,
            },
        )

        if INTENT_TAP_VERBOSE:
            try:
                dump = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            except Exception:
                dump = str(payload)
            log.debug(COMPONENT, "intent.full", extra={"envelope": dump})

        now = time.time()
        if now - last_hb >= 10.0:
            last_hb = now
            log.info(COMPONENT, "heartbeat", extra={"msg_count": msg_count})


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info(COMPONENT, "shutdown.keyboard_interrupt")
