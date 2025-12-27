import asyncio
import time
from typing import Any, Dict, Optional

import httpx

DATAHUB_URL   = "http://localhost:7000"
EVALUATOR_URL = "http://localhost:7001"

class CockpitState:
    """
    Background poller that aggregates metrics from DataHub and Evaluator
    and exposes a consistent snapshot for the cockpit UI.
    """
    def __init__(self, poll_interval_sec: float = 1.0, timeout_sec: float = 1.5) -> None:
        self._poll_interval = poll_interval_sec
        self._timeout = timeout_sec
        self._task: Optional[asyncio.Task] = None
        self._snap: Dict[str, Any] = {}
        self._client: Optional[httpx.AsyncClient] = None
        self._running = asyncio.Event()

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=self._timeout)
        self._running.set()
        self._task = asyncio.create_task(self._loop(), name="cockpit-state-poller")

    async def stop(self) -> None:
        self._running.clear()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._client:
            await self._client.aclose()
        self._client = None

    async def snapshot(self) -> Dict[str, Any]:
        # Return a shallow copy to avoid mutation races
        return dict(self._snap)

    # ---------- internals ----------
    async def _loop(self) -> None:
        while self._running.is_set():
            t0 = time.perf_counter()
            try:
                snap = await self._collect()
                snap["ts"] = time.time()
                self._snap = snap
            except Exception as e:
                # keep running even if a poll fails
                self._snap = {"ok": False, "error": repr(e), "ts": time.time()}
            # sleep the remaining of the poll interval
            dt = time.perf_counter() - t0
            await asyncio.sleep(max(0.0, self._poll_interval - dt))

    async def _collect(self) -> Dict[str, Any]:
        assert self._client is not None
        c = self._client

        async def _get_json(url: str) -> Dict[str, Any]:
            try:
                r = await c.get(url)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                return {"ok": False, "error": repr(e)}

        # DataHub
        dh_health   = await _get_json(f"{DATAHUB_URL}/health")
        dh_metrics  = await _get_json(f"{DATAHUB_URL}/metrics")     # if exposed
        dh_meta     = await _get_json(f"{DATAHUB_URL}/v1/meta")     # symbol states

        # Evaluator
        ev_health   = await _get_json(f"{EVALUATOR_URL}/health")
        ev_metrics  = await _get_json(f"{EVALUATOR_URL}/v1/metrics")
        ev_positions= await _get_json(f"{EVALUATOR_URL}/v1/positions")
        ev_orders   = await _get_json(f"{EVALUATOR_URL}/v1/orders/active")

        # Compose a cockpit snapshot (extend freely)
        return {
            "ok": True,
            "datahub": {
                "health": dh_health,
                "metrics": dh_metrics,
                "symbols": {
                    "count": len(dh_meta) if isinstance(dh_meta, dict) else None,
                    "hot": sum(1 for v in dh_meta.values() if v.get("state") == "HOT") if isinstance(dh_meta, dict) else None,
                    "cold": sum(1 for v in dh_meta.values() if v.get("state") == "COLD") if isinstance(dh_meta, dict) else None,
                },
            },
            "evaluator": {
                "health": ev_health,
                "metrics": ev_metrics,
                "positions": ev_positions,
                "orders": ev_orders,
            },
        }
