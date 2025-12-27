from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, Optional

from common import logging as log
from common.bus import subscribe, unpack, CHANNELS

COMPONENT = "debug.intent_tap"

# Use the same ORDER_CHANNEL wiring as the evaluators
ORDER_CHANNEL = CHANNELS.get("order", "eval.order_intent")

# Optional: set this env to "1" for full envelope dumps
INTENT_TAP_VERBOSE = os.getenv("INTENT_TAP_VERBOSE", "0").lower() in (
    "1",
    "true",
    "yes",
    "on",
)


async def run() -> None:
    """
    General tap on the unified intent stream.

    - Subscribes to ORDER_CHANNEL (same as eval bots).
    - Logs a compact summary for each intent.
    - Optional full envelope dump if INTENT_TAP_VERBOSE=1.
    """
    log.info(
        COMPONENT,
        "startup",
        extra={
            "order_channel": ORDER_CHANNEL,
            "verbose": INTENT_TAP_VERBOSE,
        },
    )

    msg_count = 0
    last_heartbeat = time.time()

    try:
        ps = await subscribe(ORDER_CHANNEL)
        log.info(
            COMPONENT,
            "subscribe_ok",
            extra={"channel": ORDER_CHANNEL},
        )

        async for msg in ps.listen():  # type: ignore[attr-defined]
            if msg.get("type") != "message":
                continue

            raw = msg.get("data")
            if not raw:
                continue

            try:
                payload = unpack(raw)
            except Exception:
                # fall back: if it's a raw JSON string, try to parse it
                try:
                    payload = json.loads(raw)
                except Exception:
                    log.warn(
                        COMPONENT,
                        "unpack_failed",
                        extra={"raw_type": str(type(raw))},
                    )
                    continue

            if not isinstance(payload, dict):
                log.warn(
                    COMPONENT,
                    "payload_not_dict",
                    extra={"payload_type": str(type(payload))},
                )
                continue

            msg_count += 1

            # Normal eval envelope shape: {"intent": {...}, "meta": {...}}
            intent: Dict[str, Any] = payload.get("intent") or {}
            meta: Dict[str, Any] = payload.get("meta") or {}

            symbol = (intent.get("symbol") or "").upper()
            side = (intent.get("side") or "").lower()
            qty = intent.get("qty")
            order_type = (intent.get("type") or "").lower()
            tif = (intent.get("time_in_force") or "").lower()

            # New unified routing tag
            account_tag: Optional[str] = intent.get("account_tag")
            # Backward-compat if something still sends account_id
            account_id: Optional[str] = intent.get("account_id")

            model = meta.get("model")
            strength = meta.get("strength")
            risk = meta.get("risk")

            diag = meta.get("diag") or {}

            log.info(
                COMPONENT,
                "intent.received",
                extra={
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "type": order_type,
                    "time_in_force": tif,
                    "account_tag": account_tag,
                    "account_id": account_id,
                    "model": model,
                    "strength": strength,
                    "risk": risk,
                    "msg_count": msg_count,
                },
            )

            if INTENT_TAP_VERBOSE:
                # Full envelope dump (one line JSON) for deeper debugging
                try:
                    dump = json.dumps(payload, separators=(",", ":"))
                except Exception:
                    dump = str(payload)

                log.debug(
                    COMPONENT,
                    "intent.full_envelope",
                    extra={"envelope": dump},
                )

            # Lightweight heartbeat so you see it's alive even if no intents
            now = time.time()
            if now - last_heartbeat >= 10.0:
                last_heartbeat = now
                log.info(
                    COMPONENT,
                    "heartbeat",
                    extra={"msg_count": msg_count},
                )

    except asyncio.CancelledError:
        log.info(COMPONENT, "shutdown.cancelled")
    except Exception as exc:
        log.exception(
            COMPONENT,
            "shutdown.error",
            extra={"error": repr(exc)},
        )
    finally:
        log.info(
            COMPONENT,
            "shutdown.complete",
            extra={"msg_count": msg_count},
        )


if __name__ == "__main__":
    asyncio.run(run())
