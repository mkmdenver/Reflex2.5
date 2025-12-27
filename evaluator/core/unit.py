# evaluator/core/unit.py

import asyncio
from dataclasses import dataclass, field
from typing import Dict, Any

from .drivers import LiveStreamDriver
from .state_publisher import EvalStatePublisher
from common.log import info

COMPONENT = "evaluator.unit"


@dataclass
class EvalUnitConfig:
    eval_id: str
    name: str
    mode: str  # "LIVE" or "HIST"
    kind: str  # "combo_ross", "ross_only", "filter2", etc.


class EvalUnit:
    def __init__(
        self,
        config: EvalUnitConfig,
        model,
        driver: LiveStreamDriver,
        state_publisher: EvalStatePublisher,
    ) -> None:
        self.config = config
        self.model = model
        self.driver = driver
        self.state_publisher = state_publisher
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        info(COMPONENT, "eval_unit.start", eval_id=self.config.eval_id)
        self._running = True
        self.driver.start()

    async def stop(self) -> None:
        if not self._running:
            return
        info(COMPONENT, "eval_unit.stop", eval_id=self.config.eval_id)
        self._running = False
        await self.driver.stop()

    def snapshot(self) -> Dict[str, Any]:
        # Thin wrapper – you already have EvalStatePublisher, so here we just
        # re-expose some metadata + publisher snapshot hook later.
        return {
            "eval_id": self.config.eval_id,
            "name": self.config.name,
            "mode": self.config.mode,
            "kind": self.config.kind,
            "running": self._running,
        }
