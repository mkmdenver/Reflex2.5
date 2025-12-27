import os, time
import asyncio, msgpack
from common.ipc_bus import Bus

HUB = (os.getenv("HUB_URL", "http://127.0.0.1:7000") or "").strip().rstrip("/")
SYMBOL = os.getenv("EVAL_SYMBOL", "AAPL")

async def _quotes_loop(sym: str):
    bus = Bus()
    ps = await bus.subscribe(f"reflex:rt:quotes:{sym}")
    last_ts = None
    async for m in ps.listen():
        if m.get("type") != "message":
            continue
        msg = msgpack.unpackb(m["data"], raw=False)
        q = msg.get("payload", {})
        ts = msg.get("ts_utc_ns")
        if ts != last_ts:
            last_ts = ts
            print("[Q]", sym, q.get("bid"), q.get("ask"), ts)


def main():
    try:
        asyncio.run(_quotes_loop(SYMBOL))
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
