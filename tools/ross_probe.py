# tools/ross_probe.py
import os
import sys
import asyncio
import time

# Make sure project root is on sys.path
ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common.bus import subscribe, unpack, publish_async, CHANNELS  # type: ignore


INSTANCE = os.getenv("INSTANCE", "liveA")
ACCOUNT_ID = os.getenv("ROSS_PROBE_ACCOUNT", "sim:cash")

# Should match tools/ross_filter.py
ROSS_STREAM = os.getenv(
    "ROSS_FILTER_CHANNEL",
    getattr(CHANNELS, "get", lambda *_a, **_k: None)("ross", f"reflex:{INSTANCE}:ross.warm"),
)


async def main() -> None:
    if not ROSS_STREAM:
        print("[ross_probe] ROSS_STREAM not configured; exiting")
        return

    ps = await subscribe(ROSS_STREAM)
    print(
        f"[ross_probe] instance={INSTANCE} listening on ROSS_STREAM={ROSS_STREAM} "
        f"-> orders channel={CHANNELS['order']}"
    )

    fired_symbols: set[str] = set()

    async for msg in ps.listen():
        if msg.get("type") != "message":
            continue

        ev = unpack(msg["data"])
        sym = (ev.get("symbol") or ev.get("sym") or "").upper()
        if not sym:
            continue

        if sym in fired_symbols:
            # only one intent per symbol for this probe
            continue

        price = ev.get("price") or ev.get("p")
        print(f"[ross_probe] got ross hit: {sym} price={price} -> sending intent")

        intent = {
            "symbol": sym,
            "side": "buy",
            "qty": 1,
            "type": "market",
            "time_in_force": "day",
            "account_id": ACCOUNT_ID,
            "reason": "ross_probe:ross_filter_hit",
            "ts": time.time(),
        }

        await publish_async(CHANNELS["order"], intent)
        fired_symbols.add(sym)


if __name__ == "__main__":
    asyncio.run(main())
