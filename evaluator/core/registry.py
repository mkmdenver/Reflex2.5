# evaluator/core/registry.py

from typing import Dict, List

from .unit import EvalUnit


class EvalRegistry:
    def __init__(self) -> None:
        self._evals: Dict[str, EvalUnit] = {}

    def register(self, unit: EvalUnit) -> None:
        self._evals[unit.config.eval_id] = unit

    def get(self, eval_id: str) -> EvalUnit | None:
        return self._evals.get(eval_id)

    def all(self) -> List[EvalUnit]:
        return list(self._evals.values())

    async def start_all(self) -> None:
        for unit in self._evals.values():
            await unit.start()

    async def stop_all(self) -> None:
        for unit in self._evals.values():
            await unit.stop()


registry = EvalRegistry()
