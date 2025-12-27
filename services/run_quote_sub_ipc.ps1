$env:PYTHONPATH = "$PWD"
& "$PWD\.venv\Scripts\python.exe" - << 'PYCODE'
import asyncio, msgpack
from common.ipc_bus import Bus
async def run(sym: str):
    bus = Bus()
    ps = await bus.subscribe(f"reflex:rt:quotes:{sym}")
    async for m in ps.listen():
        if m["type"] != "message": continue
        msg = msgpack.unpackb(m["data"], raw=False)
        print("QUOTE", sym, msg)
asyncio.run(run("AAPL"))
PYCODE
