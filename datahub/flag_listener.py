# datahub/flag_listener.py
import asyncio, time
from common.bus import subscribe, CHANNELS, unpack
from common.flags import FLAGS

class FlagListener:
    """Listens for admin flag changes on Redis and updates in-process cache."""
    async def run(self):
        ps = await subscribe(CHANNELS["admin_flags"])  # 'admin.flags'
        async for msg in ps.listen():
            if msg.get("type") != "message":
                continue
            ev = unpack(msg["data"])  # {symbol, DO_NOT_TRADE: bool, ts, source}
            sym = ev.get("symbol")
            if not sym:
                continue
            for k, v in ev.items():
                if k == "symbol" or k == "ts" or k == "source":
                    continue
                FLAGS.set(sym, k, v)
