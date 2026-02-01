import os
import sys
import time
import argparse

# Ensure project root is on sys.path so "common" imports work
ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common.bus import CHANNELS, publish_sync  # type: ignore


def _parse_symbols(s: str) -> list[str]:
    out: list[str] = []
    for part in (s or "").split(","):
        sym = part.strip().upper()
        if sym:
            out.append(sym)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Tier poke: publish tier changes for one or more symbols.")
    p.add_argument("--symbols", default=os.getenv("SYMBOLS", "SPY,AAPL,TSLA"),
                   help="Comma-separated symbols (default: env SYMBOLS or SPY,AAPL,TSLA)")
    p.add_argument("--tier", default="", help="Set a single tier (COLD/WATCH/WARM/HOT). If set, no cycle.")
    p.add_argument("--cycle", default="WATCH,WARM,HOT",
                   help="Cycle tiers in order (default: WATCH,WARM,HOT). Ignored if --tier is set.")
    p.add_argument("--sleep", type=float, default=2.0, help="Seconds between cycle steps (default 2.0).")
    p.add_argument("--repeat", type=int, default=1, help="How many times to repeat the cycle (default 1).")
    p.add_argument("--ts", action="store_true", help="Include ts_ms in payload (for DATAHUB_REQUIRE_TIER_TS).")
    p.add_argument("--instance", default=os.getenv("REFLEX_INSTANCE_ID", ""),
                   help="Override REFLEX_INSTANCE_ID (rare).")
    args = p.parse_args()

    # Optional instance override (handy when running from weird shells)
    if args.instance:
        os.environ["REFLEX_INSTANCE_ID"] = args.instance

    symbols = _parse_symbols(args.symbols)
    if not symbols:
        print("[tier_poke] No symbols provided.")
        return 2

    # Resolve channel after any instance override
    chan = CHANNELS["raise"]

    one_tier = (args.tier or "").strip().upper()
    if one_tier:
        tiers = [one_tier]
        repeats = 1
    else:
        tiers = [t.strip().upper() for t in (args.cycle or "").split(",") if t.strip()]
        repeats = max(1, int(args.repeat or 1))

    if not tiers:
        print("[tier_poke] No tiers specified (use --tier or --cycle).")
        return 2

    print(f"[tier_poke] ROOT={ROOT}")
    print(f"[tier_poke] channel={chan}")
    print(f"[tier_poke] symbols={symbols}")
    print(f"[tier_poke] tiers={tiers} repeat={repeats} sleep={args.sleep:.2f}s ts={bool(args.ts)}")

    def emit(sym: str, tier: str) -> None:
        payload = {"symbol": sym, "tier": tier}
        if args.ts:
            payload["ts_ms"] = int(time.time() * 1000)
        publish_sync(chan, payload)
        print(f"[tier_poke] -> {sym} {tier}")

    for r in range(repeats):
        if repeats > 1:
            print(f"[tier_poke] cycle {r + 1}/{repeats}")
        for tier in tiers:
            for sym in symbols:
                emit(sym, tier)
            if len(tiers) > 1:
                time.sleep(args.sleep)

    print("[tier_poke] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
