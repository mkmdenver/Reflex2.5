# datahub/backfill_worker.py
#
# Backfill worker: listens for backfill requests on CHANNELS["backfill"]
# and runs "today-only" backfills for tick + minute data.
#
# Payload contract (from datahub.worker.TierRequestListener):
#   {
#     "symbol": "ASNS",
#     "kinds": ["minute", "tick"],
#     "scope": "today",
#     "requested_by": "bullflag_v1",
#     "ts": 1731851234.567
#   }

from __future__ import annotations

import asyncio

from common.bus import subscribe, unpack, CHANNELS
from common.logging import info, error


# TODO: hook these up to your real backfill machinery.
# For now these are stubs so you can see the plumbing work.
async def backfill_ticks_today(symbol: str) -> None:
    info("backfill", "ticks_today.begin", symbol=symbol)
    # await db_backfill_ticks_today(symbol)
    info("backfill", "ticks_today.end", symbol=symbol)


async def backfill_minutes_today(symbol: str) -> None:
    info("backfill", "minutes_today.begin", symbol=symbol)
    # await db_backfill_minutes_today(symbol)
    info("backfill", "minutes_today.end", symbol=symbol)


async def run_backfill_worker():
    ps = await subscribe(CHANNELS["backfill"])
    info("backfill", "worker.start", channel=CHANNELS["backfill"])

    async for msg in ps.listen():
        if msg.get("type") != "message":
            continue

        ev = unpack(msg["data"]) or {}
        sym = (ev.get("symbol") or "").upper()
        kinds = ev.get("kinds") or []
        who = ev.get("requested_by") or "unknown"
        scope = ev.get("scope") or "today"

        if not sym:
            continue
        if scope != "today":
            error("backfill", f"unsupported scope={scope} for symbol={sym}")
            continue

        info("backfill", "request.received", symbol=sym, kinds=kinds, requested_by=who)

        try:
            # order can be minutes then ticks – tweak as you prefer
            if "minute" in kinds:
                await backfill_minutes_today(sym)
            if "tick" in kinds:
                await backfill_ticks_today(sym)
        except Exception as e:
            error("backfill", f"request.failed symbol={sym} err={e}")
        else:
            info("backfill", "request.complete", symbol=sym, kinds=kinds, requested_by=who)


if __name__ == "__main__":
    asyncio.run(run_backfill_worker())
