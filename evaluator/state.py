# evaluator/state.py
from __future__ import annotations

import time
from threading import RLock
from typing import Any, Dict, List


class EvaluatorState:
    """
    Thread-safe in-memory state store for the Evaluator.

    It keeps:
      • roll-up metrics (ticks_seen, decisions_made, etc.)
      • a lightweight view of open positions & active orders
      • pattern / model observations for the Cockpit panel
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._started_ts: float = time.time()
        self._last_update_ts: float | None = None

        # basic counters & metadata
        self._metrics: Dict[str, Any] = {
            "ok": True,
            "version": "evaluator-api-1",
            "ticks_seen": 0,
            "decisions_made": 0,
        }

        # very light-weight views of live positions / orders
        self._positions: List[Dict[str, Any]] = []
        self._active_orders: List[Dict[str, Any]] = []

        # pattern / model observations keyed by "<model>:<symbol>"
        self._pattern_observations: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # metrics
    # ------------------------------------------------------------------

    def update_metrics(self, **fields: Any) -> None:
        with self._lock:
            self._metrics.update(fields)
            self._last_update_ts = time.time()

    def incr_ticks(self, n: int = 1) -> None:
        with self._lock:
            self._metrics["ticks_seen"] = int(self._metrics.get("ticks_seen", 0)) + n
            self._last_update_ts = time.time()

    def incr_decisions(self, n: int = 1) -> None:
        with self._lock:
            self._metrics["decisions_made"] = int(self._metrics.get("decisions_made", 0)) + n
            self._last_update_ts = time.time()

    def snapshot_metrics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "ok": bool(self._metrics.get("ok", True)),
                "uptime_s": time.time() - self._started_ts,
                "last_update_ts": self._last_update_ts,
                **self._metrics,
            }

    # ------------------------------------------------------------------
    # positions & orders (very thin shells for now)
    # ------------------------------------------------------------------

    def set_positions(self, items: List[Dict[str, Any]]) -> None:
        with self._lock:
            self._positions = list(items)
            self._last_update_ts = time.time()

    def set_active_orders(self, items: List[Dict[str, Any]]) -> None:
        with self._lock:
            self._active_orders = list(items)
            self._last_update_ts = time.time()

    def snapshot_positions(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._positions)

    def snapshot_orders(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._active_orders)

    # ------------------------------------------------------------------
    # pattern / model observations for Cockpit
    # ------------------------------------------------------------------

    def update_pattern_observation(
        self,
        model: str,
        symbol: str,
        obs: Dict[str, Any],
    ) -> None:
        """
        Upsert the latest observation for a given (model, symbol) pair.
        """
        key = f"{model}:{symbol}".upper()
        payload = dict(obs)
        payload.setdefault("model", model)
        payload.setdefault("symbol", symbol.upper())
        with self._lock:
            self._pattern_observations[key] = payload
            self._last_update_ts = time.time()

    def snapshot_pattern_observations(self) -> List[Dict[str, Any]]:
        with self._lock:
            # return newest-first for display convenience
            return sorted(
                self._pattern_observations.values(),
                key=lambda x: x.get("updated_ts", 0.0),
                reverse=True,
            )


# global singleton the worker / model code can import and update
EVAL_STATE = EvaluatorState()
