import asyncio, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from common.bus import subscribe, unpack  # noqa

async def main():
    chan = os.getenv("CHAN", "reflex:liveA:cmd.tiers")
    ps = await subscribe(chan)
    print("[tap] subscribed:", chan)
    n = 0
    async for msg in ps.listen():
        if msg.get("type") != "message":
            continue
        n += 1
        try:
            obj = unpack(msg.get("data"))
        except Exception as e:
            print("[tap] UNPACK_FAIL:", repr(e))
            continue
        print("[tap] msg", n, obj)
        if n >= 5:
            break

if __name__ == "__main__":
    asyncio.run(main())
