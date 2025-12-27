# evaluator/mcp_orchestrator.py
from __future__ import annotations

import asyncio
import os
import signal
from typing import Optional

from common import logging as log
from evaluator.state_publisher import EvalStatePublisher

COMPONENT = "eval.mcp"


def _bool_env(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip() not in ("0", "false", "False", "no", "NO", "")


class MCPOrchestrator:
    """
    Minimal “brain stem” for the two-phase evaluator.

    This first edition only does:
      - Emit periodic heartbeat EvalState rows so EvalView has
        something to show for evalA.
    The Ross filter + pattern matcher streams will be added on
    top of this skeleton.
    """

    def __init__(self, eval_id: Optional[str] = None, intents_enabled: bool = False):
        self.eval_id = eval_id or os.getenv("EVAL_ID") or os.getenv("EVAL_INSTANCE") or "evalA"
        self.intents_enabled = intents_enabled

        # Channel for phase-1 → phase-2 handoff (future work)
        self.ross_channel = os.getenv("EVAL_ROSS_CHANNEL", "eval.ross_pillars")
        # Channel for outgoing order intents (future work)
        self.order_channel = os.getenv("EVAL_ORDER_CHANNEL", "eval.order_intent")

        self._publisher = EvalStatePublisher(eval_id=self.eval_id, module="mcp")
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        log.info(
            COMPONENT,
            "startup.begin",
            eval_id=self.eval_id,
            intents_enabled=self.intents_enabled,
            ross_channel=self.ross_channel,
            order_channel=self.order_channel,
        )

        # For now we only run a heartbeat loop. Filter/pattern loops get added later.
        self._tasks.append(asyncio.create_task(self._heartbeat_loop(), name="mcp.heartbeat"))

    async def _heartbeat_loop(self) -> None:
        """
        Emit a simple heartbeat every few seconds so EvalView can
        see that this eval instance is alive.
        """
        log.info(COMPONENT, "heartbeat.start", eval_id=self.eval_id)
        try:
            while not self._stopping.is_set():
                # One row per heartbeat: symbol="", module="mcp", phase="heartbeat"
                self._publisher.heartbeat()
                await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            # normal shutdown
            pass
        finally:
            log.info(COMPONENT, "heartbeat.stop", eval_id=self.eval_id)

    async def run(self) -> None:
        await self.start()
        await self._stopping.wait()
        # Clean up tasks
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        log.info(COMPONENT, "shutdown.complete", eval_id=self.eval_id)

    def stop(self) -> None:
        self._stopping.set()


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, mcp: MCPOrchestrator) -> None:
    def _handler():
        log.info(COMPONENT, "signal.stop", eval_id=mcp.eval_id)
        mcp.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handler)
        except NotImplementedError:
            # Windows event loop may not support this fully; best effort only.
            pass


async def _async_main() -> None:
    eval_id = os.getenv("EVAL_ID") or os.getenv("EVAL_INSTANCE") or "evalA"
    intents_enabled = _bool_env("MCP_GENERATE_INTENTS", False)

    mcp = MCPOrchestrator(eval_id=eval_id, intents_enabled=intents_enabled)

    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, mcp)

    await mcp.run()


def main() -> None:
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
