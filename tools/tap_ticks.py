# tools/tap_ticks.py

import os
import sys
import asyncio

# Ensure project root is on sys.path so "common" imports work
ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common.bus import subscribe, unpack, CHANNELS  # type: ignore


async def main():
    ps = await subscribe(CHANNELS["ticks"])
    n = 0
    print("Listening on", CHANNELS["ticks"])
    async for msg in ps.listen():
        if msg.get("type") != "message":
            continue
        ev = unpack(msg["data"])
        sym = ev.get("sym") or ev.get("symbol")
        ev_type = ev.get("ev") or ev.get("event_type") or ev.get("type")
        price = ev.get("p") or ev.get("price")
        print(f"TICK {ev_type} {sym} p={price}")
        n += 1



if __name__ == "__main__":
    asyncio.run(main())
