# scripts/smoke_feed.py
import argparse
import json
import os
import random
import time

import redis


def rcli():
    url = os.getenv("GARNET_URL", "redis://127.0.0.1:6379")
    # decode_responses=True so we publish str, not bytes
    return redis.Redis.from_url(url, decode_responses=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("rate", type=int, help="messages per second")
    ap.add_argument("seconds", type=int)
    ap.add_argument("--mode", choices=["flat", "ramp"], default="flat")
    ap.add_argument("--start", type=float, default=100.0)
    ap.add_argument("--step", type=float, default=0.05)
    ap.add_argument("--noise", type=float, default=0.01)
    args = ap.parse_args()

    sym = args.symbol.upper()
    r = rcli()
    total = max(0, args.rate * args.seconds)
    print(f"Publishing ~{args.rate} trades/sec for {args.seconds}s on md.trades.{sym} mode={args.mode}")

    price = float(args.start)
    sip0 = time.time_ns()
    sent = 0
    end_at = time.time() + args.seconds
    sleep_s = 1.0 / max(1, args.rate)

    while time.time() < end_at and sent < total:
        # emit up to <rate> msgs, then sleep a tick
        for _ in range(args.rate):
            if args.mode == "ramp":
                price += args.step + random.uniform(-args.noise, args.noise)
            p = round(price, 6)

            msg = {
                "sym": sym,
                "p": p,
                "ts": time.time(),
                # make SIP strictly unique per pub
                "sip_ts": sip0 + sent,
                "sz": 1,
                "ex": "S",
                "tape": "C",
                "cond": [],
            }
            r.publish(f"md.trades.{sym}", json.dumps(msg))
            sent += 1
            if sent >= total:
                break
        if sent >= total:
            break
        time.sleep(sleep_s)

    print(f"Done. Sent {sent} messages.")


if __name__ == "__main__":
    main()
