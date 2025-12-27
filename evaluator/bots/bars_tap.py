import asyncio, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from common.bus import subscribe  # noqa

async def main():
    chan = os.getenv("CHAN", "hub.bars1m")
    ps = await subscribe(chan)
    print("[tap] subscribed:", chan)
    n = 0
    async for msg in ps.listen():
        if msg.get("type") != "message":
            continue
        n += 1
        print("[tap] msg", n, "len=", len(msg.get("data") or b""))
        if n >= 5:
            break

if __name__ == "__main__":
    asyncio.run(main())
