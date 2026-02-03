# evaluator/bots/PTI_rbf.py
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import uuid4

import redis.asyncio as aioredis

# ---------------------------------------------------------------------------
# Repo-root bootstrap + .env/.env.local
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

COMPONENT = "eval.pti_rbf"

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
        if x > 1e18:  # ns
            return _iso_utc_from_epoch_seconds(x / 1e9)
        if x > 1e12:  # ms
            return _iso_utc_from_epoch_seconds(x / 1e3)
        return _iso_utc_from_epoch_seconds(x)

    return None


def _bar_id_minute_ms(ts_any: Any) -> Optional[int]:
    """
    Stable minute-bucket id (epoch ms) for a bar.
    Works with ms/us/ns/seconds epochs.
    """
    if ts_any is None or not isinstance(ts_any, (int, float)):
        return None

    x = int(ts_any)

    if x > 10_000_000_000_000_000:  # ns
        x //= 1_000_000
    elif x > 10_000_000_000_000:  # us
        x //= 1_000
    elif x < 10_000_000_000:  # seconds
        x *= 1000

    return (x // 60_000) * 60_000


# ---------------------------------------------------------------------------
# Small helpers
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


def _extract_bar(
    bar: Dict[str, Any],
) -> Tuple[Optional[str], Optional[Any], Optional[float], Optional[float], Optional[float], Optional[float]]:
    sym = bar.get("sym") or bar.get("symbol")
    if sym is not None:
        sym = str(sym).upper()

    o = _f(bar.get("o") or bar.get("open"))
    h = _f(bar.get("h") or bar.get("high"))
    l = _f(bar.get("l") or bar.get("low"))
    c = _f(bar.get("c") or bar.get("close"))

    ts_any = bar.get("t") or bar.get("timestamp") or bar.get("bar_ts") or bar.get("ts")
    return sym, ts_any, o, h, l, c


def _parse_filter_evt(payload: Any) -> Tuple[Optional[str], Optional[str]]:
    """
    Normalize different possible stream message shapes into:
      (kind, symbol)
    where kind ∈ {"add","remove"}
    """
    if not isinstance(payload, dict):
        return None, None

    kind = payload.get("kind") or payload.get("op") or payload.get("action") or payload.get("event")
    if isinstance(kind, str):
        kind = kind.strip().lower()
    else:
        kind = None

    sym = payload.get("symbol") or payload.get("sym") or payload.get("ticker")
    if sym is not None:
        sym = str(sym).strip().upper()
    else:
        sym = None

    if kind in ("add", "added", "insert", "join", "+"):
        kind = "add"
    elif kind in ("remove", "removed", "delete", "drop", "leave", "-"):
        kind = "remove"
    else:
        kind = None

    return kind, sym


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
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, separators=(",", ":"), default=str) + "\n")
    except Exception:
        pass


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
# Config
# ---------------------------------------------------------------------------


@dataclass
class Config:
    run_id: str
    feed_mode: str  # LIVE / REPLAY
    redis_url: str

    symbols_mode: str  # active_set|static
    symbols: List[str]
    active_set_key: str
    filter_stream_channel: str

    bars_channel: str

    # Pattern params
    min_leg_pct: float
    max_flag_retrace: float
    breakout_pct: float

    # Intent routing
    intent_mode: str  # send|record|tee|off
    intent_channel: str
    intent_record_path: str

    account_id: Optional[str]
    source: str
    strategy_id: str
    urgency: str

    gen_id: str  # one-char intent generator id

    # Risk hint knobs
    stop_buffer: float
    max_emits_per_symbol_per_minute: int

    # Optional: auto-raise tier on add (LIVE only)
    request_tier: bool
    request_tier_name: str

    # periodic resync safety net
    active_set_resync_secs: int

    stats_log_every: int

    # --- NEW: surgical debug + disk event log ---
    debug: bool
    debug_bars: bool
    debug_decisions: bool
    debug_throttle_sec: float
    event_log_enable: bool
    event_log_path: str


def load_config() -> Config:

    run_id = _env("PTI_RUN_ID")
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
        symbols_mode = _env("" \
        "", "active_set").lower()
        if symbols_mode != "active_set":
            symbols_mode = "active_set"

    active_set_key = _env("PTI_ACTIVE_SET_KEY", "eval:fts_rbf:active")
    filter_stream_channel = _env("PTI_FILTER_STREAM_CHANNEL", "eval.rbf_filter_stream")
    bars_channel = _env("PTI_BARS_CHANNEL","hub.bars1m.pub.live")
    intent_channel = _env("PTI_INTENT_CHANNEL", "eval.intent.live")
    intent_mode = _env("PTI_INTENT_MODE", "send")

    default_record = str(_repo_root / "evaluator" / "runs" / "pti_rbf" / "intents.jsonl")
    intent_record_path = _env("PTI_INTENT_RECORD_PATH", default_record)
    if intent_mode in ("record", "tee"):
        intent_record_path = _apply_record_path_stamping(intent_record_path, run_id)

    account_id = _env("PTI_ACCOUNT_ID") or None
    source = _env("PTI_SOURCE", "bot")
    strategy_id = _env("PTI_STRATEGY_ID", "pti_rbf")

    gen_id = _env("PTI_GEN_ID", "B").strip().upper()[:1] or "B"
    urgency = _env("PTI_URGENCY", "normal").lower()

    stop_buffer = float(_env("PTI_RBF_STOP_BUFFER", "0.01"))

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
    event_log_path = _resolve_path_under_root(_env("PTI_EVENT_LOG_PATH", "logs/pti_rbf_events.jsonl"))

    active_set_resync_secs = int(_env("PTI_ACTIVE_SET_RESYNC_SECS", "5"))
    if active_set_resync_secs < 0:
        active_set_resync_secs = 0

    min_leg_pct = float(_env("PTI_MIN_LEG_PCT", "0.05"))
    max_flag_retrace = float(_env("PTI_MAX_FLAG_RETRACE", "0.5"))
    breakout_pct = float(_env("PTI_BREAKOUT_PCT", "0.01"))
    max_emits_per_symbol_per_minute = int(_env("PTI_MAX_EMITS_PER_SYMBOL_PER_MINUTE", "3"))

    log.info(
        COMPONENT,
        "startup.config",
        extra={
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
            "gen_id": gen_id,
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
            "min_leg_pct": min_leg_pct,
            "max_flag_retrace": max_flag_retrace,
            "breakout_pct": breakout_pct,
            "max_emits_per_symbol_per_minute": max_emits_per_symbol_per_minute,
        },
    )

    return Config(
        run_id=run_id,
        feed_mode=feed_mode,
        redis_url=redis_url,
        symbols_mode=symbols_mode,
        symbols=symbols,
        active_set_key=active_set_key,
        filter_stream_channel=filter_stream_channel,
        bars_channel=bars_channel,
        min_leg_pct=min_leg_pct,
        max_flag_retrace=max_flag_retrace,
        breakout_pct=breakout_pct,
        intent_mode=intent_mode,
        intent_channel=intent_channel,
        intent_record_path=intent_record_path,
        account_id=account_id,
        source=source,
        strategy_id=strategy_id,
        urgency=urgency,
        gen_id=gen_id,
        stop_buffer=stop_buffer,
        max_emits_per_symbol_per_minute=max_emits_per_symbol_per_minute,
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


def _evtlog(cfg: Config, evt: Dict[str, Any]) -> None:
    if cfg.event_log_enable and cfg.event_log_path:
        _append_jsonl(cfg.event_log_path, evt)


# ---------------------------------------------------------------------------
# Tier request
# ---------------------------------------------------------------------------


async def request_tier(symbol: str, tier: str) -> None:
    evt = {
        "symbol": symbol.upper(),
        "tier": tier.upper(),
        "source": COMPONENT,
        "ts_ms": int(time.time() * 1000),
    }
    await publish_async(CHANNELS["raise"], evt)
    log.info(
        COMPONENT,
        "tier.requested",
        extra={"symbol": evt["symbol"], "tier": evt["tier"], "channel": CHANNELS["raise"]},
    )


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

        kind, sym = _parse_filter_evt(payload)
        if not kind or not sym:
            continue

        if kind == "add":
            if sym not in active:
                active.add(sym)
                log.info(COMPONENT, "filter.add", extra={"symbol": sym, "active_count": len(active)})
                _evtlog(cfg, {"kind": "universe.add", "ts": time.time(), "symbol": sym, "active_count": len(active), "source": "filter_stream"})
                if cfg.feed_mode == "LIVE" and cfg.request_tier:
                    await request_tier(sym, cfg.request_tier_name)
        elif kind == "remove":
            if sym in active:
                active.discard(sym)
                log.info(COMPONENT, "filter.remove", extra={"symbol": sym, "active_count": len(active)})
                _evtlog(cfg, {"kind": "universe.remove", "ts": time.time(), "symbol": sym, "active_count": len(active), "source": "filter_stream"})


async def _active_set_resync_loop(cfg: Config, active: Set[str]) -> None:
    """
    Safety net: periodically re-read the active set and reconcile into `active`.
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
                log.info(
                    COMPONENT,
                    "universe.resync.add",
                    extra={"count": len(added), "symbols": list(sorted(added))[:10], "active_count": len(active)},
                )
                _evtlog(cfg, {"kind": "universe.resync.add", "ts": time.time(), "count": len(added), "symbols": list(sorted(added))[:50], "active_count": len(active)})
                if cfg.feed_mode == "LIVE" and cfg.request_tier:
                    for sym in sorted(added):
                        await request_tier(sym, cfg.request_tier_name)

            if removed:
                for sym in removed:
                    active.discard(sym)
                log.info(
                    COMPONENT,
                    "universe.resync.remove",
                    extra={"count": len(removed), "symbols": list(sorted(removed))[:10], "active_count": len(active)},
                )
                _evtlog(cfg, {"kind": "universe.resync.remove", "ts": time.time(), "count": len(removed), "symbols": list(sorted(removed))[:50], "active_count": len(active)})

    finally:
        try:
            await r.aclose()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Pattern state machine (Ross Bull Flag)
# ---------------------------------------------------------------------------


class FlagState(str, Enum):
    IDLE = "idle"
    LEG_UP = "leg_up"
    FLAG = "flag"
    BROKEN = "broken"


@dataclass
class RossPatternState:
    symbol: str
    state: FlagState = FlagState.IDLE
    leg_start: Optional[float] = None
    leg_high: Optional[float] = None
    flag_low: Optional[float] = None
    last_price: Optional[float] = None
    last_update: float = 0.0

    emit_minute_ms: Optional[int] = None
    emit_count_in_minute: int = 0

    def reset(self) -> None:
        self.state = FlagState.IDLE
        self.leg_start = None
        self.leg_high = None
        self.flag_low = None
        self.last_price = None

    def _start_leg_if_needed(self, price: float) -> None:
        if self.leg_start is None:
            self.leg_start = price
        if self.leg_high is None:
            self.leg_high = price

    def update(self, close: float, high: float, cfg: Config) -> Tuple[bool, FlagState, FlagState, Dict[str, Any]]:
        """
        Returns:
          fired, old_state, new_state, diag
        diag includes computed reasons so we can explain "why not firing" without guessing.
        """
        old = self.state
        now = time.time()
        self.last_price = close
        self.last_update = now

        fired = False

        move = 0.0
        retrace = 0.0
        breakout_level = None

        if self.state == FlagState.IDLE:
            self._start_leg_if_needed(close)
            base = (self.leg_start or close)
            move = ((close - base) / base) if base > 0 else 0.0
            if move >= cfg.min_leg_pct:
                self.state = FlagState.LEG_UP
                self.leg_high = max(self.leg_high or close, close)

        elif self.state == FlagState.LEG_UP:
            self.leg_high = max(self.leg_high or close, close)
            if self.leg_high and close < self.leg_high:
                self.state = FlagState.FLAG
                self.flag_low = close

        elif self.state == FlagState.FLAG:
            if self.leg_high is None:
                self.reset()
            else:
                self.flag_low = min(self.flag_low or close, close)
                retrace = (self.leg_high - (self.flag_low or close)) / self.leg_high if self.leg_high else 0.0

                if retrace > cfg.max_flag_retrace:
                    self.state = FlagState.BROKEN

                breakout_level = self.leg_high * (1.0 + cfg.breakout_pct)

                if breakout_level is not None and high >= breakout_level:
                    fired = True
                    self.reset()

        diag = {
            "symbol": self.symbol,
            "state": self.state.value,
            "old_state": old.value,
            "leg_start": self.leg_start,
            "leg_high": self.leg_high,
            "flag_low": self.flag_low,
            "last_price": self.last_price,
            "bar_close": close,
            "bar_high": high,
            "move": move,
            "retrace": retrace,
            "breakout_level": breakout_level,
            "min_leg_pct": cfg.min_leg_pct,
            "max_flag_retrace": cfg.max_flag_retrace,
            "breakout_pct": cfg.breakout_pct,
            "ts": now,
        }
        return fired, old, self.state, diag


# ---------------------------------------------------------------------------
# Intent emission 
# ---------------------------------------------------------------------------


def _clamp01(x: float) -> float:
    if x < 0:
        return 0.0
    if x > 1.0:
        return 1.0
    return float(x)


def strength_from_breakout(leg_start: float, leg_high: float, price: float) -> float:
    if leg_start <= 0:
        return 0.5
    leg_pct = max(0.0, (leg_high - leg_start) / leg_start)
    over = max(0.0, (price - leg_high) / leg_high) if leg_high > 0 else 0.0
    s = (leg_pct / 0.06) * 0.85 + (over / 0.01) * 0.15
    return _clamp01(s)


async def emit_intent_rbf_breakout(
    cfg: Config,
    sink: IntentSink,
    sym: str,
    bar_id_ms: int,
    bar_ts_any: Any,
    o: float,
    h: float,
    l: float,
    c: float,
    diag: Dict[str, Any],
    seq: int,
) -> None:
    event_ts = _normalize_event_ts(bar_ts_any)
    emitted_ts = _iso_utc_now()
    intent_ts = event_ts or emitted_ts

    intent_id = str(uuid4())

    leg_start = _f(diag.get("leg_start")) or c
    leg_high = _f(diag.get("leg_high")) or h
    flag_low = _f(diag.get("flag_low")) or l

    strength = strength_from_breakout(leg_start, leg_high, c)

    invalidation = float(flag_low)
    initial_stop = max(0.0, float(flag_low) - cfg.stop_buffer)
    r = max(0.0, c - initial_stop)
    targets: List[Dict[str, Any]] = [{"kind": "r_multiple", "r": 1.0}, {"kind": "r_multiple", "r": 2.0}] if r > 0 else []

    signal_trigger: Dict[str, Any] = {
        "kind": "breakout",
        "basis": "1m",
        "condition": "ross_bullflag_breakout",
        "bar_id_ms": int(bar_id_ms),
        "level": float(leg_high) * (1.0 + float(cfg.breakout_pct)),
    }
    if event_ts is not None:
        signal_trigger["bar_ts"] = event_ts    # Trigger (required): LIVE => now, REPLAY => schedule at bar/event timestamp
    # Note: we keep the original breakout trigger in diag["signal_trigger"] for analysis.
    if cfg.feed_mode == "REPLAY":
        trigger: Dict[str, Any] = {
            "kind": "at_time",
            "ts": event_ts or intent_ts,
            "session": "ANY",
            "params": {
                "tolerance_ms": 500,
                "late_action": "execute",
            },
        }
    else:
        trigger = {
            "kind": "now",
            "session": "ANY",
            "params": {},
        }

    # Canonical intent models (LIVE + REPLAY share the same shape)
    models: Dict[str, Any] = {
        "entry": {"kind": "immediate", "params": {}},
        "position_mgmt": {"kind": "none", "params": {}},
        "profit": {"kind": "tslfe", "params": {}},
        "stop": {"kind": "5pt_hard", "params": {"cents": 5}},
        "exit": {"kind": "tslfe", "params": {}},
    }

    intent: Dict[str, Any] = {
        "schema": "reflex.intent.v3",
        "mode": cfg.feed_mode,
        "gen_id": cfg.gen_id,
        "intent_id": intent_id,
        "ts": intent_ts,
        "symbol": sym,
        "side": "buy",
        "source": cfg.source,
        "strategy_id": cfg.strategy_id,
        "account_id": cfg.account_id,
        "intent_key": f"{cfg.strategy_id}:{sym}:buy:breakout:1m:ross_bullflag",
        "strength": float(strength),
        "urgency": cfg.urgency,
        # --- Trade model selection (v1) ---------------------------------
        "entry_model": {"kind": "immediate"},
        "entry_model_name": "immediate",
        "position_management_model": {"kind": "none"},
        "position_mgmt_model": "none",
        "profit_model": {"kind": "tslfe"},
        "stop_model": {"kind": "5pt_hard", "cents": 5},
        "stop_loss_model": {"kind": "5pt_hard", "cents": 5},
        # Back-compat for Trader API (app.py): selects TradeRunner exit behavior
        "exit_model": "tslfe",

        "trigger": trigger,
        "models": models,
        "reason": "rbf: Ross Bull Flag breakout (leg->flag->break above leg_high)",
        "tags": ["rbf", "ross_bullflag", "1m", "breakout"],
        "risk_hints": {
            "invalidation": float(invalidation),
            "initial_stop": float(initial_stop),
            "targets": targets,
            "notes": "Non-binding risk hints; sizing comes from RiskManager.",
        },
        "diag": {
            "o": o,
            "h": h,
            "l": l,
            "c": c,
            "event_ts": event_ts,
            "emitted_ts": emitted_ts,
            "feed_mode": cfg.feed_mode,
            "bar_id_ms": int(bar_id_ms),
            "signal_trigger": signal_trigger,
            "pattern": diag,
        },
    }

    envelope = {
        "kind": "intent",
        "seq": seq,
        "ts": intent_ts,
        "event_ts": event_ts,
        "emitted_ts": emitted_ts,
        "run_id": cfg.run_id,

        "source_component": COMPONENT,
        "intent": intent,
    }

    await sink.publish(envelope)

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

    _evtlog(
        cfg,
        {
            "kind": "intent.emit",
            "ts": time.time(),
            "seq": seq,
            "intent_id": intent_id,
            "symbol": sym,
            "bar_id_ms": int(bar_id_ms),
            "event_ts": event_ts,
            "strength": float(strength),
        },
    )


# ---------------------------------------------------------------------------
# Bars loop
# ---------------------------------------------------------------------------


async def bars_loop(cfg: Config, active: Set[str], sink: IntentSink) -> None:
    ps = await subscribe(cfg.bars_channel)
    log.info(COMPONENT, "bars.subscribe_ok", extra={"channel": cfg.bars_channel})

    states: Dict[str, RossPatternState] = {}

    seq = 0
    rx = used = skipped = fires = suppressed = updates = ooo_skips = bad = 0
    skip_not_active = skip_bad_bar = skip_bad_ts = skip_ooo = 0

    last_bar_id_ms: Dict[str, int] = {}

    # throttle maps (per-symbol)
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
            bad += 1
            continue
        if not isinstance(bar, dict):
            bad += 1
            continue

        sym, ts_any, o, h, l, c = _extract_bar(bar)
        if not sym or o is None or h is None or l is None or c is None:
            skip_bad_bar += 1
            continue

        if sym not in active:
            skipped += 1
            skip_not_active += 1
            # we do NOT spam per-bar for non-active symbols; stats tell the story.
            continue

        bar_id_ms = _bar_id_minute_ms(ts_any)
        if bar_id_ms is None:
            skip_bad_ts += 1
            continue

        prev_id = last_bar_id_ms.get(sym)
        if prev_id is not None and bar_id_ms < prev_id:
            ooo_skips += 1
            skip_ooo += 1
            continue
        last_bar_id_ms[sym] = bar_id_ms

        used += 1

        # Optional: show bars being consumed (throttled)
        if cfg.debug_bars:
            now = time.time()
            last = last_dbg_bar.get(sym, 0.0)
            if (now - last) >= cfg.debug_throttle_sec:
                last_dbg_bar[sym] = now
                log.info(
                    COMPONENT,
                    "bar.used",
                    extra={"symbol": sym, "bar_id_ms": int(bar_id_ms), "o": o, "h": h, "l": l, "c": c, "active_count": len(active)},
                )
                _evtlog(cfg, {"kind": "bar.used", "ts": now, "symbol": sym, "bar_id_ms": int(bar_id_ms), "o": o, "h": h, "l": l, "c": c})

        st = states.get(sym)
        if st is None:
            st = RossPatternState(symbol=sym)
            states[sym] = st

        fired, old, new, diag = st.update(c, h, cfg)
        updates += 1

        if old != new:
            log.info(COMPONENT, "state.transition", extra={"symbol": sym, "from": old.value, "to": new.value})
            _evtlog(cfg, {"kind": "state.transition", "ts": time.time(), "symbol": sym, "from": old.value, "to": new.value})

        # If not firing, explain why (throttled + once per minute bucket)
        if cfg.debug_decisions and not fired:
            now = time.time()
            last = last_dbg_decision.get(sym, 0.0)
            minute_bucket = int(bar_id_ms // 60_000)
            last_min = last_dbg_decision_min.get(sym)

            emit = False
            if last_min != minute_bucket:
                emit = True
                last_dbg_decision_min[sym] = minute_bucket
            elif (now - last) >= cfg.debug_throttle_sec:
                emit = True

            if emit:
                last_dbg_decision[sym] = now

                state = diag.get("state")
                old_state = diag.get("old_state")
                move = diag.get("move")
                retrace = diag.get("retrace")
                breakout_level = diag.get("breakout_level")

                reason = "unknown"
                if state == FlagState.IDLE.value:
                    reason = "idle: leg not big enough yet"
                elif state == FlagState.LEG_UP.value:
                    reason = "leg_up: waiting for pullback to form flag"
                elif state == FlagState.FLAG.value:
                    if breakout_level is not None and h < breakout_level:
                        reason = "flag: not broken out yet"
                    else:
                        reason = "flag: conditions not met"
                elif state == FlagState.BROKEN.value:
                    reason = "broken: retrace exceeded max_flag_retrace"

                log.info(
                    COMPONENT,
                    "decision.no_fire",
                    extra={
                        "symbol": sym,
                        "bar_id_ms": int(bar_id_ms),
                        "state": state,
                        "old_state": old_state,
                        "reason": reason,
                        "close": c,
                        "high": h,
                        "leg_start": diag.get("leg_start"),
                        "leg_high": diag.get("leg_high"),
                        "flag_low": diag.get("flag_low"),
                        "move": move,
                        "retrace": retrace,
                        "breakout_level": breakout_level,
                        "min_leg_pct": diag.get("min_leg_pct"),
                        "max_flag_retrace": diag.get("max_flag_retrace"),
                        "breakout_pct": diag.get("breakout_pct"),
                        "active_count": len(active),
                    },
                )
                _evtlog(
                    cfg,
                    {
                        "kind": "decision.no_fire",
                        "ts": now,
                        "symbol": sym,
                        "bar_id_ms": int(bar_id_ms),
                        "state": state,
                        "reason": reason,
                        "close": c,
                        "high": h,
                        "move": move,
                        "retrace": retrace,
                        "breakout_level": breakout_level,
                    },
                )

        if fired:
            if st.emit_minute_ms != bar_id_ms:
                st.emit_minute_ms = bar_id_ms
                st.emit_count_in_minute = 0

            if st.emit_count_in_minute < cfg.max_emits_per_symbol_per_minute:
                st.emit_count_in_minute += 1
                fires += 1
                seq += 1
                await emit_intent_rbf_breakout(cfg, sink, sym, bar_id_ms, ts_any, o, h, l, c, diag, seq)
            else:
                suppressed += 1
                if cfg.debug_decisions:
                    log.info(
                        COMPONENT,
                        "intent.suppressed",
                        extra={"symbol": sym, "bar_id_ms": int(bar_id_ms), "limit": cfg.max_emits_per_symbol_per_minute},
                    )
                    _evtlog(cfg, {"kind": "intent.suppressed", "ts": time.time(), "symbol": sym, "bar_id_ms": int(bar_id_ms), "limit": cfg.max_emits_per_symbol_per_minute})

        if rx % cfg.stats_log_every == 0:
            log.info(
                COMPONENT,
                "stats",
                extra={
                    "rx": rx,
                    "used": used,
                    "skipped": skipped,
                    "active": len(active),
                    "states": len(states),
                    "fires": fires,
                    "suppressed": suppressed,
                    "ooo_skips": ooo_skips,
                    "bad": bad,
                    "skip_not_active": skip_not_active,
                    "skip_bad_bar": skip_bad_bar,
                    "skip_bad_ts": skip_bad_ts,
                    "skip_ooo": skip_ooo,
                },
            )
            _evtlog(
                cfg,
                {
                    "kind": "stats",
                    "ts": time.time(),
                    "rx": rx,
                    "used": used,
                    "skipped": skipped,
                    "active": len(active),
                    "states": len(states),
                    "fires": fires,
                    "suppressed": suppressed,
                    "ooo_skips": ooo_skips,
                    "bad": bad,
                    "skip_not_active": skip_not_active,
                    "skip_bad_bar": skip_bad_bar,
                    "skip_bad_ts": skip_bad_ts,
                    "skip_ooo": skip_ooo,
                },
            )


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
                await request_tier(sym, cfg.request_tier_name)
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
    asyncio.run(run())
