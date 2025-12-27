from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import redis.asyncio as aioredis


def _repo_root() -> Path:
    # <repo>/evaluator/bots/pti_probe.py -> parents[2] == <repo>
    return Path(__file__).resolve().parents[2]


def _load_env_file(env_path: Path) -> int:
    """Tiny .env loader.

    Returns how many KEY=VALUE lines were applied.
    """
    if not env_path.exists():
        return 0

    applied = 0
    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if not key:
            continue
        # do NOT override existing env vars
        if key not in os.environ:
            os.environ[key] = val
            applied += 1
    return applied


def _getenv(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if v else default


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="pti_probe",
        description=(
            "Bare-bones probe for Reflex2 streams: hub.bars1m, hub.ticks, "
            "and the FTS durable filter list + pubsub updates."
        ),
    )
    ap.add_argument("--env", default=str(_repo_root() / ".env"), help="Path to .env")

    # Channels
    ap.add_argument("--bars-ch", default="hub.bars1m")
    ap.add_argument("--ticks-ch", default="hub.ticks")
    ap.add_argument("--stream-ch", default="eval.fts_rbf.stream")  # pubsub for add/rem

    # Durable filter list
    ap.add_argument("--active-key", default="eval:fts_rbf:active")  # set of active symbols
    ap.add_argument(
        "--durable-key",
        default="eval:fts_rbf:stream",
        help="Durable append-only list key (Garnet-safe) produced by FTS",
    )

    # Output control
    ap.add_argument("--print-every", type=float, default=1.0, help="Seconds between status lines")
    ap.add_argument("--top", type=int, default=20, help="How many active symbols to print")
    ap.add_argument("--durable-tail", type=int, default=5, help="How many durable list events to print")
    return ap.parse_args()


def _safe_decode(b: Any) -> str:
    if b is None:
        return ""
    if isinstance(b, str):
        return b
    if isinstance(b, (bytes, bytearray, memoryview)):
        return bytes(b).decode("utf-8", errors="replace")
    return str(b)


def _parse_json_maybe(s: str) -> Dict[str, Any]:
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
        return {"value": obj}
    except Exception:
        return {"raw": s}


async def _durable_tail(r_txt: aioredis.Redis, key: str, tail: int) -> Tuple[int, List[Dict[str, Any]]]:
    """Return (len, tail_items_as_dict)."""
    try:
        n = int(await r_txt.llen(key))
    except Exception:
        return 0, []

    if n <= 0 or tail <= 0:
        return n, []

    start = max(0, n - tail)
    try:
        raw_items = await r_txt.lrange(key, start, n - 1)
    except Exception:
        return n, []

    parsed: List[Dict[str, Any]] = []
    for it in raw_items:
        s = _safe_decode(it)
        parsed.append(_parse_json_maybe(s))
    return n, parsed


def _fmt_age(now: float, ts: Optional[float]) -> str:
    if ts is None:
        return "null"
    return f"{max(0.0, now - ts):.1f}s"


async def main() -> int:
    args = _parse_args()

    repo_root = _repo_root()
    env_path = Path(args.env)

    # allow relative --env
    if not env_path.is_absolute():
        env_path = (repo_root / env_path).resolve()

    loaded = _load_env_file(env_path)

    redis_url = _getenv("REFLEX__REDIS_URL", "redis://127.0.0.1:6379/0")

    print(f"[probe] repo_root={repo_root}")
    print(f"[probe] env_path={env_path} (exists={env_path.exists()} loaded_lines={loaded})")
    print(f"[probe] redis={redis_url}")
    print(f"[probe] bars_ch={args.bars_ch}")
    print(f"[probe] ticks_ch={args.ticks_ch}")
    print(f"[probe] stream_ch={args.stream_ch}")
    print(f"[probe] active_key={args.active_key}")
    print(f"[probe] durable_key={args.durable_key}")
    print()

    # We keep TWO clients:
    # - r_bin: for pubsub (avoid implicit utf-8 decoding surprises)
    # - r_txt: for simple commands (decode_responses=True makes sets/lists easier)
    r_bin = aioredis.from_url(redis_url, decode_responses=False)
    r_txt = aioredis.from_url(redis_url, decode_responses=True)

    ps = r_bin.pubsub(ignore_subscribe_messages=True)

    # Subscriptions
    await ps.subscribe(args.bars_ch, args.ticks_ch, args.stream_ch)
    print("[probe] subscribed OK")

    bars_seen = 0
    ticks_seen = 0
    stream_seen = 0

    last_bar_ts: Optional[float] = None
    last_tick_ts: Optional[float] = None
    last_stream_ts: Optional[float] = None

    last_bar_sym = "-"
    last_tick_sym = "-"
    last_stream_sym = "-"
    last_stream_kind = "-"

    # Durable tail tracking (avoid re-printing identical tail every time)
    last_durable_print_sig = ""

    next_print = time.monotonic() + args.print_every

    try:
        while True:
            # Drain pubsub quickly
            msg = await ps.get_message(timeout=0.25)
            if msg:
                ch = _safe_decode(msg.get("channel"))
                data = msg.get("data")
                now_wall = time.time()

                if ch == args.bars_ch:
                    bars_seen += 1
                    last_bar_ts = now_wall
                    # DataHub bars are usually JSON; if not, just keep '-'
                    try:
                        d = _parse_json_maybe(_safe_decode(data))
                        sym = d.get("symbol") or d.get("sym") or d.get("s")
                        if isinstance(sym, str) and sym:
                            last_bar_sym = sym
                    except Exception:
                        pass

                elif ch == args.ticks_ch:
                    ticks_seen += 1
                    last_tick_ts = now_wall
                    try:
                        d = _parse_json_maybe(_safe_decode(data))
                        sym = d.get("symbol") or d.get("sym") or d.get("s")
                        if isinstance(sym, str) and sym:
                            last_tick_sym = sym
                    except Exception:
                        pass

                elif ch == args.stream_ch:
                    stream_seen += 1
                    last_stream_ts = now_wall
                    # Your FTS filter events are JSON: {"kind":"add|rem","symbol":"XYZ",...}
                    try:
                        d = _parse_json_maybe(_safe_decode(data))
                        kind = d.get("kind") or d.get("event") or d.get("type")
                        sym = d.get("symbol") or d.get("sym")
                        if isinstance(kind, str) and kind:
                            last_stream_kind = kind
                        if isinstance(sym, str) and sym:
                            last_stream_sym = sym
                    except Exception:
                        pass

            # Periodic status
            if time.monotonic() >= next_print:
                next_print = time.monotonic() + args.print_every
                now_wall = time.time()

                # Active set (durable/current state)
                try:
                    active_syms = await r_txt.smembers(args.active_key)
                    active_syms_sorted = sorted([s for s in active_syms if isinstance(s, str)])
                except Exception:
                    active_syms_sorted = []

                # Durable append-only list (event log)
                durable_len, durable_tail = await _durable_tail(r_txt, args.durable_key, args.durable_tail)

                # Last durable event summary
                durable_last_kind = "-"
                durable_last_sym = "-"
                durable_last_age = "null"
                if durable_tail:
                    last = durable_tail[-1]
                    k = last.get("kind") or last.get("event") or last.get("type")
                    s = last.get("symbol") or last.get("sym")
                    ts = last.get("ts")
                    if isinstance(k, str) and k:
                        durable_last_kind = k
                    if isinstance(s, str) and s:
                        durable_last_sym = s
                    if isinstance(ts, (int, float)):
                        durable_last_age = _fmt_age(now_wall, float(ts))

                print(
                    "[status] "
                    f"bars={bars_seen} (age {_fmt_age(now_wall, last_bar_ts)}) last={last_bar_sym:5s} | "
                    f"ticks={ticks_seen} (age {_fmt_age(now_wall, last_tick_ts)}) last={last_tick_sym:5s} | "
                    f"stream={stream_seen} (age {_fmt_age(now_wall, last_stream_ts)}) last={last_stream_kind}:{last_stream_sym:5s} | "
                    f"durable={durable_len} (age {durable_last_age}) last={durable_last_kind}:{durable_last_sym:5s} | "
                    f"active={len(active_syms_sorted)}"
                )

                if active_syms_sorted:
                    top = active_syms_sorted[: max(0, args.top)]
                    print("[active-top] " + " ".join(top))
                else:
                    print("[active-top] (none yet)")

                # Print durable tail only when it changes
                sig = "|".join([json.dumps(x, sort_keys=True, separators=(",", ":")) for x in durable_tail])
                if durable_tail and sig != last_durable_print_sig:
                    last_durable_print_sig = sig
                    # Render compact tail: add:XYZ rem:ABC ...
                    chunks: List[str] = []
                    for ev in durable_tail:
                        k = ev.get("kind") or ev.get("event") or ev.get("type") or "?"
                        s = ev.get("symbol") or ev.get("sym") or "?"
                        chunks.append(f"{k}:{s}")
                    print("[durable-tail] " + "  ".join(chunks))

    except KeyboardInterrupt:
        print("\n[probe] ctrl-c")
    finally:
        try:
            await ps.close()
        except Exception:
            pass
        try:
            await r_bin.close()
        except Exception:
            pass
        try:
            await r_txt.close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
