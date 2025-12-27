# tools/debug_publish_ross.py
#
# Tiny debug helper: publish a fake Ross-pillars event on eval.ross_pillars
# so we can verify that filter2_cameron_from_ross and cameron_probe
# are wired correctly.

import os
import sys
import asyncio
import time

# Ensure repo root on sys.path
HERE = os.path.dirname(__file__)
ROOT = HERE
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common.bus import publish_async  # type: ignore


async def main() -> None:
    event = {
        "symbol": "TEST",
        "price": 5.25,
        "volume": 123456,
        "ts": time.time_ns(),
        "reason": "debug_ross_pillars",
    }

    channel = "eval.ross_pillars"
    await publish_async(channel, event)
    print(f"[debug_publish_ross] published to {channel}: {event}")


if __name__ == "__main__":
    asyncio.run(main())
