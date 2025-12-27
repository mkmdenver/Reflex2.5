# datahub/tier_listener.py
import asyncio
from common.bus import subscribe, CHANNELS, unpack
from common.tiers import Tier

class TierListener:
    def __init__(self, subman):
        self.subman = subman

    async def run(self):
        ps = await subscribe(CHANNELS["raise"])
        async for msg in ps.listen():
            if msg["type"] != "message": continue
            ev = unpack(msg["data"])
            # ev = {"symbol":"BCX","tier":"HOT"}
            self.subman.apply(ev["symbol"], Tier[ev["tier"]])
