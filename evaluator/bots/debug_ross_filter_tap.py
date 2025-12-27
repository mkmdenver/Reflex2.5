from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from common import logging as log
from common.bus import subscribe, unpack

COMPONENT = "debug.ross_filter_tap"

CHANNEL = os.getenv("ROSS_FILTER_STREAM_CHANNEL", "eval.ross_filter_stream")


async def run() -> None:
    log.info(COMPONENT, "startup", extra={"channel": CHANNEL})

    try:
        ps = await subscribe(CHANNEL)
        log.info(COMPONENT, "subscribe_ok", extra={"channel": CHANNEL})
    except Exception as exc:
        log.exception(
            COMPONENT,
            "subscribe_error",
            extra={"channel": CHANNEL, "error": repr(exc)},
        )
        return

    try:
        async for msg in ps.listen():  # type: ignore[attr-defined]
            if msg.get("type") != "message":
                continue
            try:
                ev: dict[str, Any] = unpack(msg["data"])
            except Exception as exc:
                log.exception(
                    COMPONENT, "unpack_error", extra={"error": repr(exc)}
                )
                continue

            # Just dump the event nicely
            log.info(
                COMPONENT,
                "event",
                extra={"raw": json.dumps(ev)},
            )
    except asyncio.CancelledError:
        log.info(COMPONENT, "cancelled")
    except Exception as exc:
        log.exception(COMPONENT, "error", extra={"error": repr(exc)})
    finally:
        log.info(COMPONENT, "shutdown")


if __name__ == "__main__":
    asyncio.run(run())
