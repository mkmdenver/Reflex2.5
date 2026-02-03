# tools/intent_replayer.py
# Version: 2026-02-01
# Purpose:
#   Replay recorded PTI intents (JSONL) into Trader via the canonical Redis pubsub path.
#
# Design:
#   - Reads intents_*.jsonl where each line is either:
#       (A) an Intent-v1 dict   {intent_id, ts, symbol, side, strategy_id, risk_hints, ...}
#       (B) an envelope         {"intent": {...}, "meta": {...}}
#   - Publishes to the SAME channel Trader listens to (via common.bus.CHANNELS["order"]).
#   - Two modes:
#       FAST (default): emit immediately (no pacing).
#       PACED (--pace): emit according to intent time field (execute_at preferred; else ts).
#
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, Optional, Tuple

# Ensure project env is loaded consistently (.env then .env.local) without relying on shell state.
try:
    from trader.envload import load_dotenv_if_needed  # type: ignore
except Exception:
    load_dotenv_if_needed = None  # type: ignore

try:
    from common.bus import CHANNELS, publish_async  # type: ignore
except Exception:
    CHANNELS = {}  # type: ignore
    publish_async = None  # type: ignore


def _now() -> float:
    return time.time()


def _load_env() -> None:
    if load_dotenv_if_needed is None:
        return
    load_dotenv_if_needed()


def _default_instance_id() -> str:
    return os.getenv("REFLEX_INSTANCE_ID", "live")


def _resolve_order_channel(instance_id: str, override: Optional[str]) -> str:
    """
    Prefer:
      1) --channel override (explicit)
      2) common.bus.CHANNELS["order"]
      3) ORDER_CHANNEL env
      4) "eval.order_intent"
    Then apply instance scoping if the channel looks un-scoped.
    """
    if override:
        ch = override.strip()
    else:
        ch = None
        try:
            ch = CHANNELS.get("order")  # type: ignore[attr-defined]
        except Exception:
            ch = None
        if not ch:
            ch = os.getenv("ORDER_CHANNEL", "eval.order_intent")


def _default_clock_channel(instance_id: str) -> str:
    # Default replay clock channel is instance-scoped for safety.
    return f"replay.clock.{instance_id}"


def _resolve_clock_channel(instance_id: str, override: Optional[str]) -> str:
    # Prefer override, then env, then default.
    # Supports '{instance_id}' formatting.
    ch = (override or os.getenv("REPLAY_CLOCK_CHANNEL") or os.getenv("TRADER_REPLAY_CLOCK_CHANNEL") or "").strip()
    if not ch:
        return _default_clock_channel(instance_id)
    if "{instance_id}" in ch:
        return ch.format(instance_id=instance_id)
    return ch

    ch = (ch or "eval.order_intent").strip()

    if ch == "eval.order_intent":
        return f"{ch}.{instance_id}"

    if "{instance_id}" in ch:
        return ch.format(instance_id=instance_id)

    return ch


def _normalize_line(obj: Any) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    if not isinstance(obj, dict):
        return None, None

    if "intent" in obj and isinstance(obj.get("intent"), dict):
        intent = obj["intent"]
        meta = obj.get("meta") if isinstance(obj.get("meta"), dict) else {}
        return intent, meta

    return obj, {}


def _ensure_min_fields(intent: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(intent)

    if not out.get("intent_id"):
        out["intent_id"] = f"replay:{int(_now()*1000)}"

    if not out.get("ts"):
        out["ts"] = _now()

    return out


def _as_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        if isinstance(x, bool):
            return None
        return float(x)
    except Exception:
        return None


def _intent_time(intent: Dict[str, Any], preferred_field: str) -> Optional[float]:
    """
    Preferred:
      - execute_at (or caller-specified field)
      - ts
    Accepts seconds epoch (float) primarily.
    """
    # 1) explicit field
    t = _as_float(intent.get(preferred_field))
    if t:
        return float(t)

    # 2) common alternates
    for k in ("execute_at", "not_before", "scheduled_at", "enter_time", "ts"):
        t = _as_float(intent.get(k))
        if t:
            return float(t)

    return None


async def _publish_one(channel: str, intent: Dict[str, Any], meta: Optional[Dict[str, Any]]) -> None:
    if publish_async is None:
        raise RuntimeError("common.bus.publish_async is not available; cannot publish to Redis.")

    env: Dict[str, Any] = {"intent": intent}
    if meta:
        env["meta"] = meta

    await publish_async(channel, env)


async def run(argv: Optional[list[str]] = None) -> int:
    _load_env()

    ap = argparse.ArgumentParser(
        prog="intent_replayer",
        description="Replay PTI intents from JSONL into Trader via Redis (canonical intent channel).",
    )
    ap.add_argument("jsonl", help="Path to intents_*.jsonl produced by PTI.")
    ap.add_argument("--instance", default=None, help="Target instance id (e.g., replayA). Defaults to REFLEX_INSTANCE_ID.")
    ap.add_argument("--channel", default=None, help="Override Redis pubsub channel (advanced).")
    ap.add_argument("--clock-channel", default=None, help="Replay clock pulse channel (default: replay.clock.<instance>).")

    ap.add_argument("--max", type=int, default=0, help="Max lines to replay (0 = all).")
    ap.add_argument("--skip", type=int, default=0, help="Skip first N lines.")
    ap.add_argument("--dry-run", action="store_true", help="Parse and print summary, but do not publish.")

    ap.add_argument("--log-every", type=int, default=250, help="Print progress every N published intents.")
    ap.add_argument("--print-first", type=int, default=3, help="Print the first N parsed intents (sanity check).")

    # NEW: pacing
    ap.add_argument("--pace", action="store_true", help="Replay according to intent timestamps (execute_at/ts).")
    ap.add_argument("--speed", type=float, default=1.0, help="Paced speed: 1=realtime, 10=10x, 0=FAST.")
    ap.add_argument("--time-field", default="execute_at", help="Preferred time field on intent (default execute_at).")
    ap.add_argument("--start-at", type=float, default=0.0, help="Optional wall-clock epoch seconds to align replay start.")

    args = ap.parse_args(argv)

    instance_id = (args.instance or _default_instance_id()).strip()
    channel = _resolve_order_channel(instance_id, args.channel)
    clock_channel = _resolve_clock_channel(instance_id, args.clock_channel)

    path = args.jsonl
    if not os.path.exists(path):
        print(f"[ERROR] File not found: {path}", file=sys.stderr)
        return 2

    published = 0
    parsed = 0
    printed = 0

    paced = bool(args.pace) and float(args.speed or 0.0) > 0.0

    print(f"[START] intent_replayer")
    print(f"[INFO] instance={instance_id}")
    print(f"[INFO] channel={channel}")
    print(f"[INFO] clock_channel={clock_channel}")
    print(f"[INFO] file={os.path.abspath(path)}")
    print(f"[INFO] mode={'DRY_RUN' if args.dry_run else ('PACED' if paced else 'FAST')}")
    if paced:
        print(f"[INFO] pace.speed={float(args.speed)}")
        print(f"[INFO] pace.time_field={args.time_field}")

    # For paced mode:
    first_intent_ts: Optional[float] = None
    wall_start: float = float(args.start_at or 0.0) or _now()

    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            if args.skip and line_no <= int(args.skip):
                continue
            if args.max and parsed >= int(args.max):
                break

            line = line.strip()
            if not line:
                continue

            try:
                obj = json.loads(line)
            except Exception as exc:
                print(f"[WARN] line {line_no}: invalid JSON ({exc!r})", file=sys.stderr)
                continue

            intent, meta = _normalize_line(obj)
            if intent is None:
                print(f"[WARN] line {line_no}: not a dict/envelope", file=sys.stderr)
                continue

            intent = _ensure_min_fields(intent)
            parsed += 1

            if printed < int(args.print_first):
                printed += 1
                sym = intent.get("symbol")
                side = intent.get("side")
                iid = intent.get("intent_id")
                ts = intent.get("ts")
                strat = intent.get("strategy_id")
                print(f"[SAMPLE {printed}] line={line_no} intent_id={iid} symbol={sym} side={side} ts={ts} strategy_id={strat}")

            if args.dry_run:
                continue

            if paced:
                it = _intent_time(intent, str(args.time_field))
                if it is not None:
                    if first_intent_ts is None:
                        first_intent_ts = float(it)
                    # how far into the replay timeline is this intent?
                    replay_offset = max(0.0, float(it) - float(first_intent_ts))
                    # scale time by speed
                    target_wall = wall_start + (replay_offset / max(float(args.speed), 1e-9))
                    delay = target_wall - _now()
                    if delay > 0:
                        await asyncio.sleep(delay)
            # Publish replay clock pulse so Trader can use engine time deterministically.
            clock_t = _intent_time(intent, str(args.time_field))
            if clock_t is None:
                clock_t = _intent_time(intent, "ts")
            if clock_t is None:
                clock_t = _now()
            try:
                await publish_async(clock_channel, {"t": float(clock_t)})
            except Exception:
                pass

            await _publish_one(channel, intent, meta)

            published += 1
            if args.log_every and published % int(args.log_every) == 0:
                print(f"[PROGRESS] published={published} parsed={parsed} last_intent_id={intent.get('intent_id')}")

    if args.dry_run:
        print(f"[DONE] dry_run parsed={parsed} (published=0)")
        return 0

    print(f"[DONE] published={published} parsed={parsed}")
    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        print("[STOP] keyboard_interrupt")
        return 0
    except Exception as exc:
        print(f"[ERROR] crashed: {exc!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
