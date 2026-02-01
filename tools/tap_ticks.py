import os
import sys
import asyncio
from datetime import datetime, timezone

# Ensure project root is on sys.path so "common" imports work
ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common.bus import subscribe, unpack  # type: ignore


def _env(name: str, default: str) -> str:
    v = os.getenv(name)
    return v.strip() if v and v.strip() else default


def _pick_channel(mode: str) -> str:
    mode = (mode or "LIVE").upper().strip()
    if mode == "REPLAY":
        return _env("REFLEX_DATAHUB_QUOTES_PUB_REPLAY", "hub.quotes.pub.replay")
    return _env("REFLEX_DATAHUB_QUOTES_PUB_LIVE", "hub.quotes.pub.live")


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def main():
    # ultra-simple args: [LIVE|REPLAY] [--count=N] [--symbol=SPY]
    mode = "LIVE"
    count = 0  # 0 => infinite
    sym_filter = ""

    for a in sys.argv[1:]:
        aa = a.strip()
        if aa.upper() in ("LIVE", "REPLAY"):
            mode = aa.upper()
        elif aa.startswith("--count="):
            try:
                count = int(aa.split("=", 1)[1])
            except Exception:
                count = 0
        elif aa.startswith("--symbol="):
            sym_filter = aa.split("=", 1)[1].strip().upper()

    channel = _pick_channel(mode)
    ps = await subscribe(channel)

    print(f"[tap_quotes] ROOT={ROOT}")
    print(f"[tap_quotes] mode={mode}")
    print(f"[tap_quotes] channel={channel}")
    if sym_filter:
        print(f"[tap_quotes] symbol_filter={sym_filter}")
    if count:
        print(f"[tap_quotes] count={count}")
    print("[tap_quotes] waiting... (Ctrl+C to stop)")

    n = 0
    async for msg in ps.listen():
        if msg.get("type") != "message":
            continue
        ev = unpack(msg["data"])
        if not isinstance(ev, dict):
            continue

        sym = (ev.get("symbol") or ev.get("sym") or "").upper()
        if not sym:
            continue
        if sym_filter and sym != sym_filter:
            continue

        bid = ev.get("bid")
        if bid is None:
            bid = ev.get("b")
        ask = ev.get("ask")
        if ask is None:
            ask = ev.get("a")

        bs = ev.get("bid_size")
        if bs is None:
            bs = ev.get("bs")
        a_s = ev.get("ask_size")
        if a_s is None:
            a_s = ev.get("as")

        tier = ev.get("tier", "-")
        ts_ns = ev.get("sip_timestamp") or ev.get("t") or ev.get("sip_ts_ns")
        ex = ev.get("exchange", "-")

        print(f"{_now_ts()}  {sym:6}  bid={bid}x{bs}  ask={ask}x{a_s}  ex={ex}  tier={tier}  ts={ts_ns}")

        n += 1
        if count and n >= count:
            break


if __name__ == "__main__":
    asyncio.run(main())
