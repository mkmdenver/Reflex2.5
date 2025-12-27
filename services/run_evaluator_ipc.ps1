$env:PYTHONPATH = "$PWD"
& "$PWD\.venv\Scripts\python.exe" - << 'PYCODE'
import asyncio, os
from common.ipc_bus import Bus
from evaluator.ipc_emit import emit_order_intent
async def main():
    bus = Bus()
    ok = await emit_order_intent(bus, {"symbol":"AAPL","side":"BUY","qty":100,"order_type":"MKT","time_in_force":"DAY","strategy":"bull_flag_v2"})
    print("emitted?", ok)
asyncio.run(main())
PYCODE
