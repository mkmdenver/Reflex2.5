# tools/cameron_probe.py
import os
import sys
import asyncio

# Ensure project root on sys.path (same style as eval_probe.py)
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common.bus import subscribe, unpack  # type: ignore

CAMERON_CHANNEL = "eval.cameron_pillars"


async def main() -> None:
    ps = await subscribe(CAMERON_CHANNEL)
    print(f"[cameron_probe] listening on {CAMERON_CHANNEL}")
    async for raw in ps.listen():
        if raw.get("type") != "message":
            continue
        ev = unpack(raw["data"])
        print("[cameron_probe]", ev)


if __name__ == "__main__":
    asyncio.run(main())
