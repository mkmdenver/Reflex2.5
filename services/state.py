from __future__ import annotations
import asyncio, json, time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from cockpit.config import DATAHUB_BASE_URL, EVAL_BASE_URL, REDIS_URL, REFRESH_FAST_S, REFRESH_SLOW_S
from .clients import HTTPService, Bus

@dataclass
class Snapshot:
    ws_last_msg_epoch: float = 0.0
    ws_in_q_depth: float = 0.0
    ws_out_q_depth: float = 0.0
    ws_subs_count: float = 0.0
    db_last_write_epoch: float = 0.0
    eval: Dict[str, Any] = field(default_factory=lambda: {"pnl":0.0, "drawdown":0.0, "state":0})
    tiers: List[Dict[str, Any]] = field(default_factory=list)
    kpi_1m: List[Dict[str, Any]] = field(default_factory=list)
    controls: Dict[str, Any] = field(default_factory=lambda: {"throttle":100, "torque":1.0, "gear":1, "mix":"default", "paused": False})

class CockpitState:
    def __init__(self):
        self.snap = Snapshot()
        self.datahub = HTTPService(DATAHUB_BASE_URL)
        self.evaluator = HTTPService(EVAL_BASE_URL)
        self.bus = Bus(REDIS_URL)
        self._lock = asyncio.Lock()

    async def start(self):
        await self.bus.connect()
        asyncio.create_task(self._poll_fast_loop())
        asyncio.create_task(self._poll_slow_loop())
        asyncio.create_task(self._bus_listen())

    async def _poll_fast_loop(self):
        while True:
            await self.refresh_fast()
            await asyncio.sleep(REFRESH_FAST_S)

    async def _poll_slow_loop(self):
        while True:
            await self.refresh_slow()
            await asyncio.sleep(REFRESH_SLOW_S)

    async def _bus_listen(self):
        async for raw in self.bus.subscribe("evaluator.controls"):
            try:
                payload = json.loads(raw)
                async with self._lock:
                    self.snap.controls.update(payload)
            except Exception:
                pass

    async def refresh_fast(self):
        m = await self.datahub.get("/v1/metrics")
        if isinstance(m, dict):
            async with self._lock:
                for k in ("ws_last_msg_epoch","ws_in_q_depth","ws_out_q_depth","ws_subs_count","db_last_write_epoch"):
                    if k in m: setattr(self.snap, k, m[k])
                if "eval" in m: self.snap.eval = m["eval"]

        e = await self.evaluator.get("/v1/evaluator/state")
        if isinstance(e, dict):
            async with self._lock:
                self.snap.controls.update(e)

    async def refresh_slow(self):
        t = await self.datahub.get("/v1/tiers")
        if isinstance(t, dict) and "symbols" in t:
            async with self._lock:
                self.snap.tiers = t["symbols"]
        kpi = await self.datahub.get("/v1/kpi/ingest_1m?limit=120")
        if isinstance(kpi, dict) and "rows" in kpi:
            async with self._lock:
                self.snap.kpi_1m = kpi["rows"]

    async def set_controls(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        res = await self.evaluator.post("/v1/evaluator/state", payload)
        if isinstance(res, dict):
            async with self._lock:
                self.snap.controls.update(res)
            await self.bus.publish("evaluator.controls", self.snap.controls)
            return self.snap.controls
        await self.bus.publish("evaluator.controls", payload)
        async with self._lock:
            self.snap.controls.update(payload)
            return self.snap.controls

    async def snapshot(self) -> Dict[str, Any]:
        async with self._lock:
            return {
                "ws_last_msg_epoch": self.snap.ws_last_msg_epoch,
                "ws_in_q_depth": self.snap.ws_in_q_depth,
                "ws_out_q_depth": self.snap.ws_out_q_depth,
                "ws_subs_count": self.snap.ws_subs_count,
                "db_last_write_epoch": self.snap.db_last_write_epoch,
                "eval": dict(self.snap.eval),
                "tiers": list(self.snap.tiers),
                "kpi_1m": list(self.snap.kpi_1m),
                "controls": dict(self.snap.controls),
            }
