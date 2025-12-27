# evaluator/intent_tap.py
# Debug tool: print every order intent that Evaluators emit.
#
# Usage (from repo root, with .env loaded):
#   python -m evaluator.intent_tap
#
# This listens on the same Redis channel that trader/broker_worker.py uses:
#   CHANNELS["order"]
# and just logs each intent payload.

from __future__ import annotations

import asyncio

from common.bus import subscribe, CHANNELS, unpack
from common.logging import info, error

COMPONENT = "debug.intent_tap"


async def run() -> None:
    channel = CHANNELS["order"]

    info(
        COMPONENT,
        "startup.begin",
        extra={"channel": channel},
    )

    try:
        ps = await subscribe(channel)
    except Exception as exc:
        error(
            COMPONENT,
            "startup.error",
            extra={"channel": channel, "error": str(exc)},
        )
        return

    info(
        COMPONENT,
        "listen.start",
        extra={"channel": channel},
    )

    # Mirror the pattern from datahub.worker.TierRequestListener:
    async for msg in ps.listen():
        if msg.get("type") != "message":
            continue

        try:
            payload = unpack(msg["data"])
        except Exception as exc:
            error(
                COMPONENT,
                "unpack.error",
                extra={"error": str(exc), "raw": repr(msg)},
            )
            continue

        # This is the core: *just log the intent body*.
        # Trader’s broker_worker will also be seeing the same messages.
        info(
            COMPONENT,
            "intent.seen",
            extra=payload,
        )


if __name__ == "__main__":
    asyncio.run(run())
