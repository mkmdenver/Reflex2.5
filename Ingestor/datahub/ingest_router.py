# datahub/ingest_router.py
import asyncio, time
from common.bus import publisher, CHANNELS, pack

class Router:
    def __init__(self):
        self._pub = None

    async def start(self):
        self._pub = await publisher()

    async def on_tick(self, sym, tick):
        tick["t_recv_ns"] = time.time_ns()
        await self._pub.publish(CHANNELS["ticks"], pack(tick))

    async def on_quote_tob(self, sym, q):
        q["t_recv_ns"] = time.time_ns()
        await self._pub.publish(CHANNELS["quotes"], pack(q))
