import asyncio, msgpack
from common.ipc_bus import Bus

async def run(sym: str):
    bus = Bus()
    ps = await bus.subscribe(f"reflex:rt:quotes:{sym}")
    async for m in ps.listen():
        if m["type"] != "message":
            continue
        msg = msgpack.unpackb(m["data"], raw=False)
        # TODO: feed into model
        print("QUOTE", sym, msg)

if __name__ == "__main__":
    asyncio.run(run("AAPL"))
