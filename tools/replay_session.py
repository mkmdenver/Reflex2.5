# tools/replay_session.py
"""
Replay session harness: feed ticks + intents into Trader with a shared ReplayClock.

Use-case:
  - Deterministic Trader test harness for entry/exit models.
  - NOT a "fill simulator" or classic backtester.

Inputs:
  --ticks  <path.parquet>   (Polygon tick parquet)
  --intents <path.jsonl>    (intent jsonl; each line dict or envelope {"intent":...})

Timing:
  - Uses dataset timestamps (tick.t / tick.ts, intent.execute_at / intent.ts) as market time.
  - speed=0 => fastest possible (no pacing)
  - speed=1 => realtime
  - speed=10 => 10x

Channels:
  - ticks:  CHANNELS["ticks_live"] or CHANNELS["ticks"]  or env TICKS_CHANNEL or "hub.ticks"
  - intents: CHANNELS["order"] (scoped to instance) or env ORDER_CHANNEL or "eval.order_intent"

Notes:
  - Requires pyarrow for parquet streaming. (pip install pyarrow)
  - Trader must be running and listening to the same Redis.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, Iterable

# Project env load (standard Reflex)
try:
    from trader.envload import load_dotenv_if_needed  # type: ignore
except Exception:
    load_dotenv_if_needed = None  # type: ignore

try:
    from common.bus import CHANNELS, publisher, pack, publish_async  # type: ignore
except Exception:
    CHANNELS = {}  # type: ignore
    publisher = None  # type: ignore
    pack = None  # type: ignore
    publish_async = None  # type: ignore


def _load_env() -> None:
    if load_dotenv_if_needed is not None:
        load_dotenv_if_needed()


def _now() -> float:
    return time.time()


def _as_float(x: Any) -> Optional[float]:
    try:
        if x is None or isinstance(x, bool):
            return None
        return float(x)
    except Exception:
        return None


def _coerce_epoch_seconds(v: Any) -> Optional[float]:
    """
    Accept epoch in seconds, ms, us, ns.
    Heuristic by magnitude.
    """
    x = _as_float(v)
    if x is None:
        return None
    if x > 1e17:  # ns
        return x / 1e9
    if x > 1e14:  # us
        return x / 1e6
    if x > 1e11:  # ms
        return x / 1e3
    return x


# ---------------------- ReplayClock ----------------------

@dataclass
class ReplayClock:
    """
    Shared clock mapping market time -> wall time.
    """
    speed: float  # 0=fast, 1=realtime, 10=10x
    market_t0: float
    wall_t0: float

    def market_now(self) -> float:
        if self.speed <= 0:
            # Fast mode: "now" is undefined; caller shouldn't rely on it.
            return self.market_t0
        wall_elapsed = _now() - self.wall_t0
        return self.market_t0 + wall_elapsed * self.speed

    async def sleep_until_market(self, market_ts: float) -> None:
        if self.speed <= 0:
            return
        # compute wall target time
        market_offset = max(0.0, market_ts - self.market_t0)
        wall_target = self.wall_t0 + (market_offset / max(self.speed, 1e-9))
        delay = wall_target - _now()
        if delay > 0:
            await asyncio.sleep(delay)


# ---------------------- Channel resolution ----------------------

def _default_instance_id() -> str:
    return os.getenv("REFLEX_INSTANCE_ID", "live")


def _resolve_ticks_channel() -> str:
    ch = (
        CHANNELS.get("ticks_live")
        or CHANNELS.get("ticks")
        or os.getenv("TICKS_CHANNEL")
        or "hub.ticks"
    )
    return str(ch).strip()


def _resolve_intents_channel(instance_id: str, override: Optional[str]) -> str:
    """
    Mirror tools/intent_replayer logic: base channel + instance suffix if needed.
    """
    if override:
        ch = override.strip()
    else:
        ch = None
        try:
            ch = CHANNELS.get("order")
        except Exception:
            ch = None
        if not ch:
            ch = os.getenv("ORDER_CHANNEL", "eval.order_intent")

    ch = (ch or "eval.order_intent").strip()
    if ch == "eval.order_intent":
        return f"{ch}.{instance_id}"
    if "{instance_id}" in ch:
        return ch.format(instance_id=instance_id)
    return ch


# ---------------------- Parquet streaming ----------------------

def _iter_parquet_rows(path: str) -> Iterable[Dict[str, Any]]:
    try:
        import pyarrow.parquet as pq  # type: ignore
    except Exception as exc:
        raise RuntimeError("pyarrow is required (pip install pyarrow)") from exc

    pf = pq.ParquetFile(path)
    for batch in pf.iter_batches(batch_size=50_000):
        cols = batch.schema.names
        arrays = [batch.column(i).to_pylist() for i in range(batch.num_columns)]
        for i in range(batch.num_rows):
            yield {cols[c]: arrays[c][i] for c in range(len(cols))}


def _row_to_tick(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    sym = (row.get("symbol") or row.get("sym") or "")
    sym = str(sym).upper().strip()
    if not sym:
        return None

    price = _as_float(row.get("price") if "price" in row else row.get("p"))
    size = row.get("size") if "size" in row else row.get("s")
    try:
        size_i = int(size)
    except Exception:
        size_i = None

    if price is None or size_i is None:
        return None

    # tick timestamp fields vary across Polygon exports
    ts = None
    for k in ("t", "ts", "timestamp", "sip_timestamp", "participant_timestamp", "trf_timestamp"):
        if k in row:
            ts = _coerce_epoch_seconds(row.get(k))
            if ts is not None:
                break

    out = {"symbol": sym, "p": float(price), "s": int(size_i)}
    if ts is not None:
        out["t"] = float(ts)  # seconds epoch (we'll pack it; consumers can handle)
    # exchange optional
    exch = row.get("exchange") if "exchange" in row else row.get("x")
    try:
        if exch is not None:
            out["x"] = int(exch)
    except Exception:
        pass
    return out


# ---------------------- Intent JSONL ----------------------

def _normalize_intent_line(obj: Any) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    if not isinstance(obj, dict):
        return None, None
    if "intent" in obj and isinstance(obj.get("intent"), dict):
        meta = obj.get("meta") if isinstance(obj.get("meta"), dict) else {}
        return obj["intent"], meta
    return obj, {}


def _intent_time(intent: Dict[str, Any]) -> Optional[float]:
    # prefer execute_at, else ts
    for k in ("execute_at", "not_before", "scheduled_at", "ts"):
        t = _coerce_epoch_seconds(intent.get(k))
        if t is not None:
            return float(t)
    return None


# ---------------------- Replay tasks ----------------------

async def _replay_ticks(clock: ReplayClock, ticks_path: str, ticks_channel: str, speed: float, max_rows: int) -> None:
    if publisher is None or pack is None:
        raise RuntimeError("common.bus.publisher/pack unavailable")

    pub = await publisher()

    sent = 0
    for row in _iter_parquet_rows(ticks_path):
        tick = _row_to_tick(row)
        if not tick:
            continue

        t = _coerce_epoch_seconds(tick.get("t"))
        if t is not None:
            await clock.sleep_until_market(float(t))

        await pub.publish(ticks_channel, pack(tick))
        sent += 1
        if max_rows and sent >= max_rows:
            break

    print(f"[ticks] done sent={sent}")


async def _replay_intents(clock: ReplayClock, intents_path: str, intents_channel: str, max_lines: int) -> None:
    if publish_async is None:
        raise RuntimeError("common.bus.publish_async unavailable")

    sent = 0
    with open(intents_path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception:
                continue

            intent, meta = _normalize_intent_line(obj)
            if not intent:
                continue

            it = _intent_time(intent)
            if it is not None:
                await clock.sleep_until_market(float(it))

            env: Dict[str, Any] = {"intent": intent}
            if meta:
                env["meta"] = meta

            await publish_async(intents_channel, env)
            sent += 1
            if max_lines and sent >= max_lines:
                break

    print(f"[intents] done sent={sent}")


# ---------------------- Main ----------------------

async def main() -> int:
    _load_env()

    ap = argparse.ArgumentParser(description="Replay ticks + intents into Trader with a shared ReplayClock.")
    ap.add_argument("--ticks", required=True, help="Tick parquet file path")
    ap.add_argument("--intents", required=True, help="Intent jsonl file path")
    ap.add_argument("--speed", type=float, default=10.0, help="0=fast, 1=realtime, 10=10x")
    ap.add_argument("--instance", default=None, help="REFLEX_INSTANCE_ID target (for intent channel scoping)")
    ap.add_argument("--intents-channel", default=None, help="Override intent pubsub channel")
    ap.add_argument("--max-ticks", type=int, default=0, help="Max ticks to send (0=all)")
    ap.add_argument("--max-intents", type=int, default=0, help="Max intents to send (0=all)")
    args = ap.parse_args()

    ticks_path = args.ticks
    intents_path = args.intents
    if not os.path.exists(ticks_path):
        raise SystemExit(f"[ERROR] ticks not found: {ticks_path}")
    if not os.path.exists(intents_path):
        raise SystemExit(f"[ERROR] intents not found: {intents_path}")

    instance_id = (args.instance or _default_instance_id()).strip()
    ticks_channel = _resolve_ticks_channel()
    intents_channel = _resolve_intents_channel(instance_id, args.intents_channel)

    # Determine a common market_t0 so ticks + intents align:
    # - For simplicity: use earliest tick timestamp we encounter (first valid tick row).
    #   This avoids loading the full file.
    first_tick_ts = None
    for row in _iter_parquet_rows(ticks_path):
        tick = _row_to_tick(row)
        if tick and tick.get("t") is not None:
            first_tick_ts = _coerce_epoch_seconds(tick.get("t"))
            if first_tick_ts is not None:
                break
    if first_tick_ts is None:
        raise SystemExit("[ERROR] could not find a usable timestamp in ticks parquet")

    market_t0 = float(first_tick_ts)
    wall_t0 = _now()

    clock = ReplayClock(speed=float(args.speed), market_t0=market_t0, wall_t0=wall_t0)

    print("[replay_session] START")
    print(f"[replay_session] speed={args.speed}")
    print(f"[replay_session] ticks={os.path.abspath(ticks_path)}")
    print(f"[replay_session] intents={os.path.abspath(intents_path)}")
    print(f"[replay_session] ticks_channel={ticks_channel}")
    print(f"[replay_session] intents_channel={intents_channel}")
    print(f"[replay_session] market_t0={market_t0}")

    t1 = asyncio.create_task(_replay_ticks(clock, ticks_path, ticks_channel, float(args.speed), int(args.max_ticks or 0)))
    t2 = asyncio.create_task(_replay_intents(clock, intents_path, intents_channel, int(args.max_intents or 0)))

    await asyncio.gather(t1, t2)
    print("[replay_session] DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
