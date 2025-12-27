# tools/eval_probe.py
import os
import sys
import asyncio
import time

# Make sure project root is on sys.path
ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common.bus import subscribe, unpack, publish_async, CHANNELS  # type: ignore


TARGET_SYMBOL = os.getenv("EVAL_PROBE_SYMBOL", "SPY").upper()
ACCOUNT_ID = os.getenv("EVAL_PROBE_ACCOUNT", "sim:cash")


async def main():
    ps = await subscribe(CHANNELS["ticks"])
    print(f"[eval_probe] listening on {CHANNELS['ticks']} for {TARGET_SYMBOL}")

    fired = False

    async for msg in ps.listen():
        if msg.get("type") != "message":
            continue

        ev = unpack(msg["data"])
        sym = (ev.get("sym") or ev.get("symbol") or "").upper()
        if sym != TARGET_SYMBOL:
            continue

        price = ev.get("p") or ev.get("price")
        print(f"[eval_probe] got tick {sym} p={price}")

        if fired:
            continue  # only send one intent

        intent = {
            "symbol": TARGET_SYMBOL,
            "side": "buy",
            "qty": 1,
            "type": "market",
            "time_in_force": "day",
            "account_id": ACCOUNT_ID,
            "reason": "eval_probe:first_tick",
            "ts": time.time(),
        }

        print(f"[eval_probe] publishing intent: {intent}")
        await publish_async(CHANNELS["order"], intent)
        fired = True


if __name__ == "__main__":
    asyncio.run(main())
