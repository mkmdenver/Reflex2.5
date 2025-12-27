# tools/tap_quotes.py

import os
import sys
import asyncio

# Ensure project root is on sys.path so "common" imports work
ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common.bus import subscribe, unpack, CHANNELS  # type: ignore


async def main():
    ps = await subscribe(CHANNELS["quotes"])
    n = 0
    print("Listening on", CHANNELS["quotes"])
    async for msg in ps.listen():
        if msg.get("type") != "message":
            continue
        ev = unpack(msg["data"])
        sym = ev.get("sym") or ev.get("symbol")
        ev_type = ev.get("ev") or ev.get("event_type") or ev.get("type")
        bid = ev.get("b") or ev.get("bid")
        ask = ev.get("a") or ev.get("ask")
        print(f"QUOTE {ev_type} {sym} bid={bid} ask={ask}")
        n += 1
        if n >= 50:
            break


if __name__ == "__main__":
    asyncio.run(main())
