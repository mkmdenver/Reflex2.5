from __future__ import annotations

import asyncio
import os
import time
from typing import Dict, Any, Optional

import json
import redis.asyncio as aioredis

from common import logging as log

from . import FTS_simple1
from . import PSI_simple1

COMPONENT = "eval.simple_combo"

# Shared telemetry config (matches FTS_simple1 / PSI_simple1 / EvalView)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
EVAL_STATE_CHANNEL = os.getenv("EVAL_STATE_CHANNEL", "eval.state")
EVAL_STATE_INTERVAL = float(os.getenv("EVAL_STATE_INTERVAL", "5.0"))


class EvalTelemetry:
    """
    Very lightweight heartbeat publisher so EvalView can show that this
    combo wrapper itself is alive, in addition to the two sub-modules.
    """

    def __init__(self, eval_id: str, modules: list[str]) -> None:
        self.eval_id = eval_id
        self.modules = modules
        self.rows = len(modules)
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


async def run() -> None:
    """
    Simple "combo" wrapper: runs the simple filter and simple pattern
    evaluators in one process. This is optional sugar – the individual
    bots can still be run separately.
    """
    modules = ["filter_to_stream", "pattern_from_stream"]

    log.info(
        COMPONENT,
        "startup",
        extra={"modules": modules},
    )

    telemetry = EvalTelemetry("CTI_simple1", modules)
    stop_event = asyncio.Event()

    async def telemetry_loop() -> None:
        while not stop_event.is_set():
            await telemetry.maybe_publish(
                {
                    "wrapper": "running",
                }
            )
            await asyncio.sleep(1.0)

    filter_task = asyncio.create_task(FTS_simple1.run())
    pattern_task = asyncio.create_task(PSI_simple1.run())
    telemetry_task = asyncio.create_task(telemetry_loop())

    try:
        await asyncio.wait(
            {filter_task, pattern_task},
            return_when=asyncio.FIRST_EXCEPTION,
        )
    finally:
        stop_event.set()
        for t in (filter_task, pattern_task, telemetry_task):
            if not t.done():
                t.cancel()
        log.info(COMPONENT, "shutdown.complete")


if __name__ == "__main__":
    asyncio.run(run())
