# evaluator/bots/PTI_template.py
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple, List
from uuid import uuid4

import redis.asyncio as aioredis

# ---------------------------------------------------------------------------
# repo-root sys.path bootstrap + .env/.env.local loader (KISS, canonical)
# ---------------------------------------------------------------------------

_THIS = Path(__file__).resolve()
_repo_root: Path | None = None
for p in [_THIS.parent, *_THIS.parents]:
    if (p / ".env").exists():
        _repo_root = p
        break
if _repo_root is None:
    _repo_root = _THIS.parents[2]

if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))


def _load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and (k not in os.environ):
                os.environ[k] = v
    except Exception:
        # KISS: never crash on env loading
        pass


_load_dotenv_file(_repo_root / ".env")
_load_dotenv_file(_repo_root / ".env.local")

from common import logging as log
from common.bus import CHANNELS, publish_async, subscribe, unpack

COMPONENT = "eval.pti_template"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _f(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None


def _iso_utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int((time.time() % 1) * 1000):03d}Z"


def _extract_bar(bar: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[float], Optional[float], Optional[float], Optional[float]]:
    sym = bar.get("sym") or bar.get("symbol")
    if sym:
        sym = str(sym).upper()

    o = _f(bar.get("o") or bar.get("open"))
    h = _f(bar.get("h") or bar.get("high"))
    l = _f(bar.get("l") or bar.get("low"))
    c = _f(bar.get("c") or bar.get("close"))

    bt = bar.get("t") or bar.get("ts") or bar.get("bar_ts")
    bar_ts = str(bt) if bt is not None else None
    return sym, bar_ts, o, h, l, c


# ---------------------------------------------------------------------------
# Intent sinks (output switch)
# ---------------------------------------------------------------------------

class IntentSink:
    async def publish(self, envelope: Dict[str, Any]) -> None:
        raise NotImplementedError


class SendSink(IntentSink):
    def __init__(self, channel: str) -> None:
        self.channel = channel

    async def publish(self, envelope: Dict[str, Any]) -> None:
        await publish_async(self.channel, envelope)


class RecordSink(IntentSink):
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def publish(self, envelope: Dict[str, Any]) -> None:
        # Append-only JSONL
        line = json.dumps(envelope, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()


class TeeSink(IntentSink):
    def __init__(self, a: IntentSink, b: IntentSink) -> None:
        self.a = a
        self.b = b

    async def publish(self, envelope: Dict[str, Any]) -> None:
        await self.a.publish(envelope)
        await self.b.publish(envelope)


class OffSink(IntentSink):
    async def publish(self, envelope: Dict[str, Any]) -> None:
        return


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Config:
    redis_url: str

    # Membership / universe
    active_set_key: str
    filter_stream_channel: str

    # Feed (LIVE vs REPLAY should be solved by pointing this at the right channel)
    bars_channel: str

    # Output mode
    intent_channel: str
    intent_mode: str               # send|record|tee|off
    intent_record_path: str

    # Intent metadata
    source: str
    strategy_id: str
    urgency: str
    account_id: Optional[str]

    # Bot behavior
    cooldown_sec: float
    stop_buffer: float
    stats_log_every: int


def load_config() -> Config:
    redis_url = (
        os.getenv("GARNET_URL")
        or os.getenv("REDIS_URL")
        or os.getenv("REFLEX_REDIS_URL")
        or "redis://127.0.0.1:6379/0"
    )

    active_set_key = os.getenv("PTI_ACTIVE_SET_KEY", "eval:fts_rbf:active")
    filter_stream_channel = os.getenv("PTI_FILTER_STREAM_CHANNEL", "eval.ross_filter_stream")

    # Choose LIVE vs REPLAY by switching this env var in .env.local or runner scripts
    bars_channel = os.getenv("PTI_BARS_CHANNEL", os.getenv("REFLEX_DATAHUB_BARS1M_PUB_LIVE", "hub.bars1m"))

    intent_channel = os.getenv("PTI_INTENT_CHANNEL", CHANNELS.get("order", "eval.order_intent"))
    intent_mode = (os.getenv("PTI_INTENT_MODE", "send") or "send").strip().lower()
    if intent_mode not in ("send", "record", "tee", "off"):
        intent_mode = "send"

    intent_record_path = os.getenv("PTI_INTENT_RECORD_PATH", str(_repo_root / "runs" / "pti" / "intents.jsonl"))

    source = os.getenv("PTI_SOURCE", "bot")
    strategy_id = os.getenv("PTI_STRATEGY_ID", "pti_template")
    urgency = (os.getenv("PTI_URGENCY", "normal") or "normal").strip().lower()

    account_id = (os.getenv("PTI_ACCOUNT_ID", "") or "").strip() or None

    cooldown_sec = float(os.getenv("PTI_COOLDOWN_SEC", "10"))
    stop_buffer = float(os.getenv("PTI_STOP_BUFFER", "0.01"))
    stats_log_every = int(os.getenv("PTI_STATS_LOG_EVERY", "200"))

    log.info(COMPONENT, "startup.config", extra={
        "redis_url": redis_url,
        "active_set_key": active_set_key,
        "filter_stream_channel": filter_stream_channel,
        "bars_channel": bars_channel,
        "intent_channel": intent_channel,
        "intent_mode": intent_mode,
        "intent_record_path": intent_record_path,
        "strategy_id": strategy_id,
        "account_id": account_id,
        "cooldown_sec": cooldown_sec,
    })

    return Config(
        redis_url=redis_url,
        active_set_key=active_set_key,
        filter_stream_channel=filter_stream_channel,
        bars_channel=bars_channel,
        intent_channel=intent_channel,
        intent_mode=intent_mode,
        intent_record_path=intent_record_path,
        source=source,
        strategy_id=strategy_id,
        urgency=urgency,
        account_id=account_id,
        cooldown_sec=cooldown_sec,
        stop_buffer=stop_buffer,
        stats_log_every=stats_log_every,
    )


def make_sink(cfg: Config) -> IntentSink:
    if cfg.intent_mode == "off":
        return OffSink()
    if cfg.intent_mode == "send":
        return SendSink(cfg.intent_channel)
    if cfg.intent_mode == "record":
        return RecordSink(cfg.intent_record_path)
    # tee
    return TeeSink(SendSink(cfg.intent_channel), RecordSink(cfg.intent_record_path))


# ---------------------------------------------------------------------------
# Membership management (FTS stream controls universe)
# ---------------------------------------------------------------------------

async def bootstrap_active_set(cfg: Config) -> Set[str]:
    r = aioredis.from_url(cfg.redis_url, decode_responses=True)
    try:
        members = await r.smembers(cfg.active_set_key)
        active = {m.upper() for m in members}
        log.info(COMPONENT, "active.bootstrap", extra={"count": len(active), "key": cfg.active_set_key})
        return active
    finally:
        try:
            await r.aclose()
        except Exception:
            pass


async def filter_stream_loop(cfg: Config, active: Set[str]) -> None:
    ps = await subscribe(cfg.filter_stream_channel)
    log.info(COMPONENT, "filter.subscribe_ok", extra={"channel": cfg.filter_stream_channel})

    async for msg in ps.listen():  # type: ignore[attr-defined]
        if msg.get("type") != "message":
            continue
        try:
            payload = unpack(msg["data"])
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue

        kind = payload.get("kind")
        sym = payload.get("symbol")
        if not sym:
            continue
        sym = str(sym).upper()

        if kind == "add":
            if sym not in active:
                active.add(sym)
                log.info(COMPONENT, "filter.add", extra={"symbol": sym, "active_count": len(active)})
        elif kind == "remove":
            if sym in active:
                active.discard(sym)
                log.info(COMPONENT, "filter.remove", extra={"symbol": sym, "active_count": len(active)})


# ---------------------------------------------------------------------------
# Pattern example (1-bar-up on 1m bars, intentionally simple)
# Replace this logic per model; keep the template structure stable.
# ---------------------------------------------------------------------------

def strength_from_bar(o: float, c: float) -> float:
    if o <= 0:
        return 0.5
    body_pct = abs(c - o) / o
    s = body_pct / 0.005
    return 0.0 if s < 0 else 1.0 if s > 1.0 else s


async def emit_intent(
    cfg: Config,
    sink: IntentSink,
    sym: str,
    bar_ts: Optional[str],
    o: float, h: float, l: float, c: float,
    prev_close: Optional[float],
) -> None:
    intent_id = str(uuid4())
    ts = _iso_utc_now()
    strength = strength_from_bar(o, c)

    trigger: Dict[str, Any] = {"kind": "bar_close", "basis": "1m", "condition": "up"}
    if bar_ts is not None:
        trigger["bar_ts"] = bar_ts

    invalidation = l
    initial_stop = max(0.0, l - cfg.stop_buffer)
    r = max(0.0, c - initial_stop)
    targets: List[Dict[str, Any]] = [{"kind": "r_multiple", "r": 1.0}, {"kind": "r_multiple", "r": 2.0}] if r > 0 else []

    intent: Dict[str, Any] = {
        "intent_id": intent_id,
        "ts": ts,
        "symbol": sym,
        "side": "buy",
        "source": cfg.source,
        "strategy_id": cfg.strategy_id,
        "account_id": cfg.account_id,
        "intent_key": f"{cfg.strategy_id}:{sym}:buy:bar_close:1m",
        "strength": float(strength),
        "urgency": cfg.urgency,
        "trigger": trigger,
        "reason": "template: 1m bar closed up",
        "tags": ["template", "1m", "bar_close", "up"],
        "context": {"timeframe": "1m"},
        "risk_hints": {
            "invalidation": float(invalidation),
            "initial_stop": float(initial_stop),
            "targets": targets,
            "notes": "Non-binding risk hints; sizing comes from RiskManager.",
        },
        "diag": {"o": o, "h": h, "l": l, "c": c, "prev_close": prev_close},
    }

    envelope = {"intent": intent, "meta": {"model": cfg.strategy_id}}
    await sink.publish(envelope)

    log.info(COMPONENT, "intent.emitted", extra={"intent_id": intent_id, "symbol": sym, "strength": strength, "mode": cfg.intent_mode})


async def bars_loop(cfg: Config, active: Set[str], sink: IntentSink) -> None:
    ps = await subscribe(cfg.bars_channel)
    log.info(COMPONENT, "bars.subscribe_ok", extra={"channel": cfg.bars_channel})

    last_close: Dict[str, float] = {}
    last_fire_ts: Dict[str, float] = {}

    rx = used = skipped = fires = 0

    async for msg in ps.listen():  # type: ignore[attr-defined]
        if msg.get("type") != "message":
            continue
        rx += 1

        try:
            bar = unpack(msg["data"])
        except Exception:
            continue
        if not isinstance(bar, dict):
            continue

        sym, bar_ts, o, h, l, c = _extract_bar(bar)
        if not sym or o is None or h is None or l is None or c is None:
            continue

        if sym not in active:
            skipped += 1
            continue

        used += 1
        prev = last_close.get(sym)
        last_close[sym] = c

        # Example "1 bar up" condition (close > open)
        is_up = c > o

        if is_up:
            now = time.time()
            last = last_fire_ts.get(sym, 0.0)
            if (now - last) >= cfg.cooldown_sec:
                last_fire_ts[sym] = now
                fires += 1
                await emit_intent(cfg, sink, sym, bar_ts, o, h, l, c, prev)

        if rx % cfg.stats_log_every == 0:
            log.info(COMPONENT, "stats", extra={"rx": rx, "used": used, "skipped": skipped, "active": len(active), "fires": fires})


async def run() -> None:
    cfg = load_config()
    sink = make_sink(cfg)

    active = await bootstrap_active_set(cfg)

    tasks = [
        asyncio.create_task(filter_stream_loop(cfg, active), name="pti.filter"),
        asyncio.create_task(bars_loop(cfg, active, sink), name="pti.bars"),
    ]

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    for t in pending:
        t.cancel()
    for t in done:
        exc = t.exception()
        if exc:
            raise exc


if __name__ == "__main__":
    asyncio.run(run())
