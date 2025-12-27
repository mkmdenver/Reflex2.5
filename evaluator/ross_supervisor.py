# evaluator/ross_supervisor.py

import asyncio
from evaluator.model_tiers import ModelTierState
from evaluator.model_combo_ross_bullflag import ComboRossBullFlagModel
from evaluator.ross_filters import run_filter1, check_filter2
from evaluator.service_tiers import request_service_level

class RossSupervisor:

    def __init__(self, name="ross_combo"):
        self.name = name
        self.tiers = ModelTierState(name)
        self.model = ComboRossBullFlagModel(name)

    async def initialize(self):
        universe = await run_filter1()
        for sym in universe:
            self.tiers.set(sym, "M0_FUNDAMENTAL")

    async def filter2_loop(self):
        while True:
            await self.run_filter2_pass()
            await asyncio.sleep(60)

    async def run_filter2_pass(self):
        for sym in self.tiers.symbols_in("M0_FUNDAMENTAL", "M1_LIVE_CANDIDATE"):
            passed = await check_filter2(sym)

            if passed:
                if self.tiers.get(sym) != "M1_LIVE_CANDIDATE":
                    self.tiers.set(sym, "M1_LIVE_CANDIDATE")
                    request_service_level(sym, "WARM", source=self.name)
            else:
                self.tiers.set(sym, "M0_FUNDAMENTAL")

    async def on_trade(self, ev):
        sym = ev["symbol"]

        if self.tiers.get(sym) not in ("M1_LIVE_CANDIDATE", "M2_PATTERN_FOUND", "M3_ARMED"):
            return

        result = await self.model.on_trade(ev)

        if result == "PATTERN_FOUND":
            self.tiers.set(sym, "M2_PATTERN_FOUND")
            request_service_level(sym, "HOT", source=self.name)

        elif result == "ARMED":
            self.tiers.set(sym, "M3_ARMED")

        elif result == "TRIGGERED":
            self.tiers.set(sym, "M4_TRIGGERED")
