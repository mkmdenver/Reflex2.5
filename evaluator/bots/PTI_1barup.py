# evaluator/bots/PTI_1barup.py
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import uuid4

import redis.asyncio as aioredis

# ---------------------------------------------------------------------------
# Repo-root bootstrap + .env/.env.local (canonical)
# ---------------------------------------------------------------------------

_THIS = Path(__file__).resolve()
_repo_root: Optional[Path] = None
for p in [_THIS.parent, *_THIS.parents]:
    if (p / ".env").exists():
        _repo_root = p
        break
if _repo_root is None:
    _repo_root = _THIS.parents[2]

if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))


from common import logging as log  # noqa: E402
from common.bus import CHANNELS, publish_async, subscribe, unpack  # noqa: E402

COMPONENT = "eval.pti_1barup"

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _iso_utc_now() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(dt.microsecond/1000):03d}Z"


def _iso_utc_from_epoch_seconds(sec: float) -> str:
    dt = datetime.fromtimestamp(sec, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(dt.microsecond/1000):03d}Z"


def _normalize_event_ts(ts_any: Any) -> Optional[str]:
    if ts_any is None:
        return None

    if isinstance(ts_any, str):
        s = ts_any.strip()
        if not s:
            return None
        s2 = s.replace(" ", "T")
        if s2.endswith("Z"):
            return s2 if "." in s2 else (s2[:-1] + ".000Z")
        if s2.endswith("+00:00"):
            s2 = s2[:-6] + "Z"
            return s2 if "." in s2 else (s2[:-1] + ".000Z")
        return s2

    if isinstance(ts_any, (int, float)):
        x = float(ts_any)
        if x <= 0:
            return None
        if x > 1e18:
            return _iso_utc_from_epoch_seconds(x / 1e9)
        if x > 1e12:
            return _iso_utc_from_epoch_seconds(x / 1e3)
        return _iso_utc_from_epoch_seconds(x)

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return v.strip() if v and v.strip() else default


def _truthy(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in ("1", "true", "t", "yes", "y", "on"):
        return True
    if s in ("0", "false", "f", "no", "n", "off", ""):
        return False
    return default


def _with_instance_suffix(channel: str, instance_id: str, feed_mode: str) -> str:
    """
    Enforce instance-scoped intent channels in LIVE so Trader (liveA) sees them.

    - Supports '{instance_id}' placeholder.
    - If PTI_INTENT_CHANNEL is a bare base (eval.intent / manual.intent / eval.order_intent), auto-suffix.
    - In REPLAY, leave channel as-is unless templated.
    """
    ch = (channel or "").strip()
    if not ch:
        return ch

    inst = (instance_id or "").strip()
    mode = (feed_mode or "").strip().upper()

    if "{instance_id}" in ch:
        return ch.replace("{instance_id}", inst)

    if mode != "LIVE" or not inst:
        return ch

    if ch.endswith(f".{inst}"):
        return ch

    if ch in ("eval.intent", "manual.intent", "eval.order_intent"):
        return f"{ch}.{inst}"

    if ch.startswith("eval.intent.") or ch.startswith("manual.intent.") or ch.startswith("eval.order_intent."):
        return ch

    return ch


def _resolve_path_under_root(path: str) -> str:
    p = (path or "").strip()
    if not p:
        return ""
    if os.path.isabs(p):
        return p
    return str((_repo_root / p).resolve())


def _append_jsonl(path: str, obj: Dict[str, Any]) -> None:
    # Never throw from logging.
    try:
        pp = Path(path)
        pp.parent.mkdir(parents=True, exist_ok=True)
        with pp.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, separators=(",", ":"), default=str) + "\n")
    except Exception:
        pass


def _evtlog(cfg: "Config", evt: Dict[str, Any]) -> None:
    if cfg.event_log_enable and cfg.event_log_path:
        _append_jsonl(cfg.event_log_path, evt)


def _csv_syms(s: str) -> List[str]:
    out: List[str] = []
    for part in (s or "").replace(";", ",").split(","):
        p = part.strip().upper()
        if p:
            out.append(p)
    return out


def _f(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None


def _extract_bar(bar: Dict[str, Any]) -> Tuple[Optional[str], Optional[Any], Optional[float], Optional[float], Optional[float], Optional[float]]:
    sym = bar.get("sym") or bar.get("symbol")
    if sym is not None:
        sym = str(sym).upper()

    o = _f(bar.get("o") or bar.get("open"))
    h = _f(bar.get("h") or bar.get("high"))
    l = _f(bar.get("l") or bar.get("low"))
    c = _f(bar.get("c") or bar.get("close"))

    ts_any = bar.get("t") or bar.get("timestamp") or bar.get("bar_ts") or bar.get("ts")
    return sym, ts_any, o, h, l, c


def _bar_id_minute_ms(ts_any: Any) -> Optional[int]:
    """
    Return a stable minute-bucket id (epoch ms) for a bar.
    Works with ms/us/ns/seconds epochs.
    """
    if ts_any is None or not isinstance(ts_any, (int, float)):
        return None

    x = int(ts_any)

    # normalize to ms
    if x > 10_000_000_000_000_000:  # ns
        x = x // 1_000_000
    elif x > 10_000_000_000_000:  # us
        x = x // 1_000
    elif x < 10_000_000_000:  # seconds
        x = x * 1000

    return (x // 60_000) * 60_000


# ---------------------------------------------------------------------------
# Intent sinks
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


def _stamp_suffix() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _apply_record_path_stamping(path: str, run_id: str) -> str:
    ts = _stamp_suffix()
    out = (path or "").replace("{run_id}", run_id).replace("{ts}", ts)
    if out != path:
        return out
    if out.lower().endswith(".jsonl"):
        base = out[:-5].rstrip("._-")
        return f"{base}_{ts}.jsonl"
    return out


# ---------------------------------------------------------------------------
# Config (canonical + debug/event log)
# ---------------------------------------------------------------------------


@dataclass
class Config:
    instance_id: str
    run_id: str
    feed_mode: str  # LIVE / REPLAY
    redis_url: str

    symbols_mode: str  # active_set|static
    symbols: List[str]
    active_set_key: str
    filter_stream_channel: str

    bars_channel: str

    intent_mode: str  # send|record|tee|off
    intent_channel: str
    intent_record_path: str

    account_id: Optional[str]
    source: str
    strategy_id: str
    urgency: str

    stop_buffer: float

    request_tier: bool
    request_tier_name: str

    stats_log_every: int

    # debug/event log
    debug: bool
    debug_bars: bool
    debug_decisions: bool
    debug_throttle_sec: float
    event_log_enable: bool
    event_log_path: str

    # active-set resync safety net
    active_set_resync_secs: int


def load_config() -> Config:
    instance_id = _env("REFLEX_INSTANCE_ID", "liveA")

    run_id = _env("REFLEX_RUN_ID")
    if not run_id:
        run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    feed_mode = _env("PTI_FEED_MODE", "LIVE").upper()
    if feed_mode not in ("LIVE", "REPLAY"):
        feed_mode = "LIVE"

    redis_url = _env("GARNET_URL") or _env("REDIS_URL", "redis://127.0.0.1:6379/0")

    symbols = _csv_syms(_env("PTI_SYMBOLS", ""))
    if symbols:
        symbols_mode = "static"
    else:
        symbols_mode = _env("PTI_SYMBOLS_MODE", "active_set").lower()
        if symbols_mode != "active_set":
            symbols_mode = "active_set"

    active_set_key = _env("PTI_ACTIVE_SET_KEY", "eval:fts_1barup:active")
    filter_stream_channel = _env("PTI_FILTER_STREAM_CHANNEL", "eval.1barup_filter_stream")

    bars_channel = _env("PTI_BARS_CHANNEL")
    if not bars_channel:
        bars_channel = _env(
            "REFLEX_DATAHUB_BARS1M_PUB_REPLAY" if feed_mode == "REPLAY" else "REFLEX_DATAHUB_BARS1M_PUB_LIVE",
            "hub.bars1m.pub.replay" if feed_mode == "REPLAY" else "hub.bars1m.pub.live",
        )

    intent_channel = _env("PTI_INTENT_CHANNEL", "eval.intent")
    intent_channel = _with_instance_suffix(intent_channel, instance_id, feed_mode)

    intent_mode = _env("PTI_INTENT_MODE", "send").lower()
    if intent_mode not in ("send", "record", "tee", "off"):
        intent_mode = "send"

    default_record = str(_repo_root / "evaluator" / "runs" / "pti_1barup" / "intents.jsonl")
    intent_record_path = _env("PTI_INTENT_RECORD_PATH", default_record)
    if intent_mode in ("record", "tee"):
        intent_record_path = _apply_record_path_stamping(intent_record_path, run_id)

    account_id = _env("PTI_ACCOUNT_ID") or None
    source = _env("PTI_SOURCE", "bot")
    strategy_id = _env("PTI_STRATEGY_ID", "pti_1barup")
    urgency = _env("PTI_URGENCY", "normal").lower()

    stop_buffer = float(_env("PTI_1BAR_STOP_BUFFER", "0.01"))

    request_tier = _env("PTI_REQUEST_TIER", "1").lower() in ("1", "true", "yes", "on")
    request_tier_name = _env("PTI_REQUEST_TIER_NAME", "WATCH").upper()
    if request_tier_name not in ("COLD", "WATCH", "WARM", "HOT"):
        request_tier_name = "WATCH"

    stats_log_every = int(_env("PTI_STATS_LOG_EVERY", "200"))

    debug = _truthy(_env("PTI_DEBUG", "0"))
    debug_bars = _truthy(_env("PTI_DEBUG_BARS", "0")) or debug
    debug_decisions = _truthy(_env("PTI_DEBUG_DECISIONS", "0")) or debug
    debug_throttle_sec = float(_env("PTI_DEBUG_THROTTLE_SEC", "10"))

    event_log_enable = _truthy(_env("PTI_EVENT_LOG_ENABLE", "0"))
    event_log_path = _resolve_path_under_root(_env("PTI_EVENT_LOG_PATH", "logs/pti_1barup_events.jsonl"))

    active_set_resync_secs = int(_env("PTI_ACTIVE_SET_RESYNC_SECS", "5"))
    if active_set_resync_secs < 0:
        active_set_resync_secs = 0

    log.info(
        COMPONENT,
        "startup.config",
        extra={
            "instance_id": instance_id,
            "run_id": run_id,
            "feed_mode": feed_mode,
            "redis_url": redis_url,
            "symbols_mode": symbols_mode,
            "symbols": symbols,
            "active_set_key": active_set_key,
            "filter_stream_channel": filter_stream_channel,
            "bars_channel": bars_channel,
            "intent_mode": intent_mode,
            "intent_channel": intent_channel,
            "intent_record_path": intent_record_path,
            "strategy_id": strategy_id,
            "account_id": account_id,
            "request_tier": request_tier,
            "request_tier_name": request_tier_name,
            "debug": debug,
            "debug_bars": debug_bars,
            "debug_decisions": debug_decisions,
            "debug_throttle_sec": debug_throttle_sec,
            "event_log_enable": event_log_enable,
            "event_log_path": event_log_path if event_log_enable else None,
            "active_set_resync_secs": active_set_resync_secs,
        },
    )

    return Config(
        instance_id=instance_id,
        run_id=run_id,
        feed_mode=feed_mode,
        redis_url=redis_url,
        symbols_mode=symbols_mode,
        symbols=symbols,
        active_set_key=active_set_key,
        filter_stream_channel=filter_stream_channel,
        bars_channel=bars_channel,
        intent_mode=intent_mode,
        intent_channel=intent_channel,
        intent_record_path=intent_record_path,
        account_id=account_id,
        source=source,
        strategy_id=strategy_id,
        urgency=urgency,
        stop_buffer=stop_buffer,
        request_tier=request_tier,
        request_tier_name=request_tier_name,
        stats_log_every=stats_log_every,
        debug=debug,
        debug_bars=debug_bars,
        debug_decisions=debug_decisions,
        debug_throttle_sec=debug_throttle_sec,
        event_log_enable=event_log_enable,
        event_log_path=event_log_path,
        active_set_resync_secs=active_set_resync_secs,
    )


def make_sink(cfg: Config) -> IntentSink:
    if cfg.intent_mode == "off":
        return OffSink()
    if cfg.intent_mode == "send":
        return SendSink(cfg.intent_channel)
    if cfg.intent_mode == "record":
        return RecordSink(cfg.intent_record_path)
    return TeeSink(SendSink(cfg.intent_channel), RecordSink(cfg.intent_record_path))


# ---------------------------------------------------------------------------
# Tier request
# ---------------------------------------------------------------------------


async def request_tier(symbol: str, tier: str, instance_id: str) -> None:
    evt = {
        "symbol": symbol.upper(),
        "tier": tier.upper(),
        "source": COMPONENT,
        "instance_id": instance_id,
        "ts_ms": int(time.time() * 1000),
    }
    await publish_async(CHANNELS["raise"], evt)
    log.info(COMPONENT, "tier.requested", extra={"symbol": evt["symbol"], "tier": evt["tier"], "channel": CHANNELS["raise"]})
    # note: no evtlog here; it’s a command, not a PTI decision


# ---------------------------------------------------------------------------
# Universe control
# ---------------------------------------------------------------------------


async def _bootstrap_active_set(cfg: Config) -> Set[str]:
    r = aioredis.from_url(cfg.redis_url, decode_responses=True)
    try:
        members = await r.smembers(cfg.active_set_key)
        active = {str(m).upper() for m in members}
        log.info(COMPONENT, "universe.active_set.bootstrap", extra={"count": len(active), "key": cfg.active_set_key})
        _evtlog(cfg, {"kind": "universe.bootstrap", "ts": time.time(), "count": len(active), "key": cfg.active_set_key})
        return active
    finally:
        try:
            await r.aclose()
        except Exception:
            pass


async def _filter_stream_loop(cfg: Config, active: Set[str]) -> None:
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
                _evtlog(cfg, {"kind": "universe.add", "ts": time.time(), "symbol": sym, "active_count": len(active), "source": "filter_stream"})
                if cfg.feed_mode == "LIVE" and cfg.request_tier:
                    await request_tier(sym, cfg.request_tier_name, cfg.instance_id)

        elif kind == "remove":
            if sym in active:
                active.discard(sym)
                log.info(COMPONENT, "filter.remove", extra={"symbol": sym, "active_count": len(active)})
                _evtlog(cfg, {"kind": "universe.remove", "ts": time.time(), "symbol": sym, "active_count": len(active), "source": "filter_stream"})


async def _active_set_resync_loop(cfg: Config, active: Set[str]) -> None:
    """
    Safety net: periodically re-read the active set and reconcile into `active`.
    Helpful if the filter stream misses deltas or PTI starts before FTS populates.
    """
    if cfg.active_set_resync_secs <= 0:
        return

    r = aioredis.from_url(cfg.redis_url, decode_responses=True)
    try:
        while True:
            await asyncio.sleep(cfg.active_set_resync_secs)
            try:
                members = await r.smembers(cfg.active_set_key)
                now_set = {str(m).upper() for m in members}
            except Exception:
                continue

            added = now_set - active
            removed = active - now_set

            if added:
                for sym in sorted(added):
                    active.add(sym)
                log.info(COMPONENT, "universe.resync.add", extra={"count": len(added), "symbols": list(sorted(added))[:10], "active_count": len(active)})
                _evtlog(cfg, {"kind": "universe.resync.add", "ts": time.time(), "count": len(added), "symbols": list(sorted(added))[:50], "active_count": len(active)})
                if cfg.feed_mode == "LIVE" and cfg.request_tier:
                    for sym in sorted(added):
                        await request_tier(sym, cfg.request_tier_name, cfg.instance_id)

            if removed:
                for sym in removed:
                    active.discard(sym)
                log.info(COMPONENT, "universe.resync.remove", extra={"count": len(removed), "symbols": list(sorted(removed))[:10], "active_count": len(active)})
                _evtlog(cfg, {"kind": "universe.resync.remove", "ts": time.time(), "count": len(removed), "symbols": list(sorted(removed))[:50], "active_count": len(active)})

    finally:
        try:
            await r.aclose()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Model: closed-up vs previous close (bar-close only, finalize on next bar)
# ---------------------------------------------------------------------------


def strength_from_close(prev_close: float, close: float) -> float:
    if prev_close <= 0:
        return 0.5
    body_pct = abs(close - prev_close) / prev_close
    s = body_pct / 0.005
    if s < 0:
        return 0.0
    if s > 1.0:
        return 1.0
    return float(s)


async def emit_intent_closed_up(
    cfg: Config,
    sink: IntentSink,
    sym: str,
    bar_id_ms: int,
    bar_ts_any: Any,
    o: float,
    h: float,
    l: float,
    c: float,
    prev_close: float,
    seq: int,
) -> None:
    event_ts = _normalize_event_ts(bar_ts_any)
    emitted_ts = _iso_utc_now()

    intent_id = str(uuid4())
    strength = strength_from_close(prev_close, c)
    intent_ts = event_ts or emitted_ts

    invalidation = l
    initial_stop = max(0.0, l - cfg.stop_buffer)
    r = max(0.0, c - initial_stop)
    targets: List[Dict[str, Any]] = [{"kind": "r_multiple", "r": 1.0}, {"kind": "r_multiple", "r": 2.0}] if r > 0 else []

    trigger: Dict[str, Any] = {
        "kind": "immediate",
        "basis": "1m",
        "condition": "close_gt_prev_close",
        "bar_id_ms": int(bar_id_ms),
    }
    if event_ts is not None:
        trigger["bar_ts"] = event_ts

    # --- NEW canonical model fields (v1) ---
    # Entry is immediate for now. Profit uses TSLFE. Stop-loss is 5pt hard.
    intent: Dict[str, Any] = {
        "intent_id": intent_id,
        "ts": intent_ts,
        "symbol": sym,
        "side": "buy",
        "source": cfg.source,
        "strategy_id": cfg.strategy_id,
        "account_id": cfg.account_id,
        "intent_key": f"{cfg.strategy_id}:{sym}:buy:bar_close:1m:close_gt_prev_close",
        "strength": float(strength),
        "urgency": cfg.urgency,
        "trigger": trigger,
        "reason": "1barup: 1m bar CLOSED up vs previous close (finalized on next bar)",
        "tags": ["1barup", "1m", "bar_close", "close_gt_prev_close"],

        # model selection consumed by Trader (exit_model used by our patched Trader)
        "entry_model": "immediate",
        "position_mgmt_model": "none",
        "profit_model": "tslfe",
        "stop_loss_model": "5pt_hard",
        "exit_model": "tslfe",

        "risk_hints": {
            "invalidation": float(invalidation),
            "initial_stop": float(initial_stop),
            "targets": targets,
            "notes": "Non-binding risk hints; sizing comes from RiskManager.",
            # non-breaking, additional hints
            "profit_model": "tslfe",
            "stop_loss_model": "5pt_hard",
        },
        "diag": {
            "o": o,
            "h": h,
            "l": l,
            "c": c,
            "prev_close": prev_close,
            "event_ts": event_ts,
            "emitted_ts": emitted_ts,
            "feed_mode": cfg.feed_mode,
            "bar_id_ms": int(bar_id_ms),
        },
    }

    envelope = {
        "kind": "intent",
        "seq": seq,
        "ts": intent_ts,
        "event_ts": event_ts,
        "emitted_ts": emitted_ts,
        "run_id": cfg.run_id,
        "instance_id": cfg.instance_id,
        "source_component": COMPONENT,
        "intent": intent,
    }

    await sink.publish(envelope)

    _evtlog(
        cfg,
        {
            "kind": "intent.emit",
            "ts": time.time(),
            "seq": seq,
            "intent_id": intent_id,
            "symbol": sym,
            "strength": float(strength),
            "channel": cfg.intent_channel,
            "bar_id_ms": int(bar_id_ms),
            "event_ts": event_ts,
        },
    )

    log.info(
        COMPONENT,
        "intent.emit",
        extra={
            "seq": seq,
            "intent_id": intent_id,
            "symbol": sym,
            "strength": strength,
            "mode": cfg.intent_mode,
            "channel": cfg.intent_channel,
            "record_path": cfg.intent_record_path if cfg.intent_mode in ("record", "tee") else None,
            "event_ts": event_ts,
            "bar_id_ms": int(bar_id_ms),
        },
    )


async def bars_loop(cfg: Config, active: Set[str], sink: IntentSink) -> None:
    ps = await subscribe(cfg.bars_channel)
    log.info(COMPONENT, "bars.subscribe_ok", extra={"channel": cfg.bars_channel})

    # per-symbol current bar snapshot keyed by minute bucket
    curr_bar_id: Dict[str, int] = {}
    curr_bar: Dict[str, Tuple[Any, float, float, float, float]] = {}  # ts_any, o,h,l,c

    # per-symbol last finalized close and last finalized bar id
    last_final_close: Dict[str, float] = {}
    last_final_bar_id: Dict[str, int] = {}

    # ensure we emit at most once per finalized bar
    emitted_final: Set[Tuple[str, int]] = set()

    seq = 0
    rx = used = skipped = fires = updates = finalizes = dup_final_skips = ooo_skips = 0

    # debug throttles
    last_dbg_bar: Dict[str, float] = {}
    last_dbg_decision: Dict[str, float] = {}
    last_dbg_decision_min: Dict[str, int] = {}

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

        sym, ts_any, o, h, l, c = _extract_bar(bar)
        if not sym or o is None or h is None or l is None or c is None:
            continue

        if sym not in active:
            skipped += 1
            continue

        bar_id_ms = _bar_id_minute_ms(ts_any)
        if bar_id_ms is None:
            continue

        used += 1

        # Optional: show bars being consumed (throttled per symbol)
        if cfg.debug_bars:
            now = time.time()
            last = last_dbg_bar.get(sym, 0.0)
            if (now - last) >= cfg.debug_throttle_sec:
                last_dbg_bar[sym] = now
                log.info(COMPONENT, "bar.used", extra={"symbol": sym, "bar_id_ms": int(bar_id_ms), "o": o, "h": h, "l": l, "c": c, "active_count": len(active)})
                _evtlog(cfg, {"kind": "bar.used", "ts": now, "symbol": sym, "bar_id_ms": int(bar_id_ms), "o": o, "h": h, "l": l, "c": c})

        prev_curr_id = curr_bar_id.get(sym)

        # Out-of-order protection: if we've already finalized beyond this bar, ignore it.
        last_fin_id = last_final_bar_id.get(sym)
        if last_fin_id is not None and bar_id_ms <= last_fin_id:
            ooo_skips += 1
            continue

        if prev_curr_id is None:
            curr_bar_id[sym] = bar_id_ms
            curr_bar[sym] = (ts_any, o, h, l, c)
            continue

        if bar_id_ms == prev_curr_id:
            updates += 1
            curr_bar[sym] = (ts_any, o, h, l, c)
            continue

        if bar_id_ms < prev_curr_id:
            ooo_skips += 1
            continue

        # bar_id_ms > prev_curr_id: minute rolled -> finalize previous minute bar
        prev_snapshot = curr_bar.get(sym)
        if prev_snapshot is not None:
            prev_ts_any, prev_o, prev_h, prev_l, prev_c = prev_snapshot
            finalize_id = prev_curr_id

            fin_key = (sym, finalize_id)
            if fin_key not in emitted_final:
                prev_close = last_final_close.get(sym)
                if prev_close is not None:
                    if prev_c > prev_close:
                        seq += 1
                        fires += 1
                        emitted_final.add(fin_key)
                        await emit_intent_closed_up(
                            cfg, sink, sym, finalize_id, prev_ts_any,
                            prev_o, prev_h, prev_l, prev_c, prev_close, seq
                        )
                    else:
                        # explain why we didn't fire (throttled; once per finalized minute)
                        if cfg.debug_decisions:
                            now = time.time()
                            minute_bucket = int(finalize_id // 60_000)
                            last_min = last_dbg_decision_min.get(sym)
                            emit = False
                            if last_min != minute_bucket:
                                emit = True
                                last_dbg_decision_min[sym] = minute_bucket
                            elif (now - last_dbg_decision.get(sym, 0.0)) >= cfg.debug_throttle_sec:
                                emit = True
                            if emit:
                                last_dbg_decision[sym] = now
                                log.info(
                                    COMPONENT,
                                    "decision.no_fire",
                                    extra={"symbol": sym, "bar_id_ms": int(finalize_id), "reason": "prev_c_not_gt_prev_close", "prev_close": float(prev_close), "prev_c": float(prev_c)},
                                )
                                _evtlog(cfg, {"kind": "decision.no_fire", "ts": now, "symbol": sym, "bar_id_ms": int(finalize_id), "reason": "prev_c_not_gt_prev_close", "prev_close": float(prev_close), "prev_c": float(prev_c)})
                else:
                    # We don't have a previous close yet; seed only.
                    if cfg.debug_decisions:
                        now = time.time()
                        if (now - last_dbg_decision.get(sym, 0.0)) >= cfg.debug_throttle_sec:
                            last_dbg_decision[sym] = now
                            log.info(COMPONENT, "decision.seed", extra={"symbol": sym, "bar_id_ms": int(finalize_id), "reason": "no_prev_close"})
                            _evtlog(cfg, {"kind": "decision.seed", "ts": now, "symbol": sym, "bar_id_ms": int(finalize_id), "reason": "no_prev_close"})
            else:
                dup_final_skips += 1

            # finalize bookkeeping
            last_final_close[sym] = prev_c
            last_final_bar_id[sym] = finalize_id
            finalizes += 1

        # start tracking the new current minute
        curr_bar_id[sym] = bar_id_ms
        curr_bar[sym] = (ts_any, o, h, l, c)

        if rx % cfg.stats_log_every == 0:
            log.info(
                COMPONENT,
                "stats",
                extra={
                    "rx": rx,
                    "used": used,
                    "skipped": skipped,
                    "active": len(active),
                    "fires": fires,
                    "updates": updates,
                    "finalizes": finalizes,
                    "dup_final_skips": dup_final_skips,
                    "ooo_skips": ooo_skips,
                },
            )
            _evtlog(cfg, {"kind": "stats", "ts": time.time(), "rx": rx, "used": used, "skipped": skipped, "active": len(active), "fires": fires, "updates": updates, "finalizes": finalizes, "dup_final_skips": dup_final_skips, "ooo_skips": ooo_skips})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run() -> None:
    cfg = load_config()
    sink = make_sink(cfg)

    if cfg.symbols_mode == "static":
        active = set(cfg.symbols)
        log.info(COMPONENT, "universe.static", extra={"count": len(active), "symbols": cfg.symbols})
        _evtlog(cfg, {"kind": "universe.static", "ts": time.time(), "count": len(active), "symbols": list(cfg.symbols)[:200]})
        if cfg.feed_mode == "LIVE" and cfg.request_tier:
            for sym in sorted(active):
                await request_tier(sym, cfg.request_tier_name, cfg.instance_id)
    else:
        active = await _bootstrap_active_set(cfg)
        log.info(COMPONENT, "universe.active_set", extra={"count": len(active)})
        _evtlog(cfg, {"kind": "universe.active_set", "ts": time.time(), "count": len(active)})

    tasks: List[asyncio.Task] = []
    if cfg.symbols_mode == "active_set":
        tasks.append(asyncio.create_task(_filter_stream_loop(cfg, active), name="pti.filter"))
        tasks.append(asyncio.create_task(_active_set_resync_loop(cfg, active), name="pti.resync"))
    tasks.append(asyncio.create_task(bars_loop(cfg, active, sink), name="pti.bars"))

    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for t in done:
            exc = t.exception()
            if exc:
                raise exc
    except asyncio.CancelledError:
        log.info(COMPONENT, "shutdown.cancelled")
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info(COMPONENT, "shutdown.complete")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info(COMPONENT, "shutdown.keyboard_interrupt")
