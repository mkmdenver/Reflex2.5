# evaluator/loop.py
import asyncio
from common.bus import subscribe, CHANNELS, unpack
from evaluator.model_bullflag import BullFlagModel

async def run():
    ps_ticks  = await subscribe(CHANNELS["ticks"])
    ps_quotes = await subscribe(CHANNELS["quotes"])

    model = BullFlagModel(name="bullflag_v1")

    async def pump_ticks():
        async for msg in ps_ticks.listen():
            if msg.get("type") != "message":
                continue
            ev = unpack(msg["data"])
            await model.on_trade(ev)

    async def pump_quotes():
        async for msg in ps_quotes.listen():
            if msg.get("type") != "message":
                continue
            ev = unpack(msg["data"])
            await model.on_quote_tob(ev)

    await asyncio.gather(pump_ticks(), pump_quotes())

if __name__ == "__main__":
    asyncio.run(run())
