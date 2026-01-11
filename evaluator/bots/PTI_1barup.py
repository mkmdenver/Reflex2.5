# evaluator/bots/PTI_1barup.py
from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import uuid4

import redis.asyncio as aioredis

# ---------------------------------------------------------------------------
# repo-root sys.path bootstrap + .env/.env.local loader (KISS)
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
        pass


_load_dotenv_file(_repo_root / ".env")
_load_dotenv_file(_repo_root / ".env.local")

from common import logging as log
from common.bus import CHANNELS, publish_async, subscribe, unpack

COMPONENT = "eval.pti_1barup"


def _csv_syms(raw: str) -> List[str]:
    out: List[str] = []
    for part in (raw or "").split(","):
        s = part.strip().upper()
        if s:
            out.append(s)
    # de-dupe preserve order
    seen: Set[str] = set()
    uniq: List[str] = []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


@dataclass
class Config:
    redis_url: str

    symbols: List[str]
    symbols_mode: str  # "static" or "active_set"
    active_set_key: str
    filter_stream_channel: str

    warm_bars_channel: str

    up_mode: str
    cooldown_sec: float
    stop_buffer: float

    # monotonic promotion only
    request_warm_tier: bool
    warm_tier_name: str

    intent_channel: str
    account_id: Optional[str]
    source: str
    strategy_id: str
    urgency: str
    stats_log_every: int


def load_config() -> Config:
    redis_url = (
        os.getenv("GARNET_URL")
        or os.getenv("REDIS_URL")
        or os.getenv("REFLEX_REDIS_URL")
        or "redis://127.0.0.1:6379/0"
    )

    # IMPORTANT: no built-in defaults; empty means dynamic/active_set mode
    symbols = _csv_syms(os.getenv("PTI_1BAR_SYMBOLS", ""))
    symbols_mode = "static" if symbols else "active_set"

    active_set_key = os.getenv("ROSS_ACTIVE_KEY", "eval:fts_rbf:active")
    filter_stream_channel = os.getenv("ROSS_FILTER_STREAM_CHANNEL", "eval.ross_filter_stream")
    warm_bars_channel = os.getenv("REFLEX_DATAHUB_BARS1M_PUB_LIVE", "hub.bars1m")

    up_mode = (os.getenv("PTI_1BAR_UP_MODE", "closeopen") or "closeopen").strip().lower()
    if up_mode not in ("closeopen", "prevclose"):
        up_mode = "closeopen"

    cooldown_sec = float(os.getenv("PTI_COOLDOWN_SEC", "10"))
    stop_buffer = float(os.getenv("PTI_1BAR_STOP_BUFFER", "0.01"))

    request_warm_tier = os.getenv("PTI_1BAR_REQUEST_WARM_TIER", "1").strip().lower() in ("1", "true", "yes", "on")
    warm_tier_name = (os.getenv("PTI_1BAR_WARM_TIER_NAME", "WARM") or "WARM").strip().upper()

    intent_channel = os.getenv("PTI_INTENT_CHANNEL", CHANNELS.get("order", "eval.order_intent"))

    account_id = (os.getenv("PTI_ACCOUNT_ID", "") or "").strip()
    if not account_id:
        account_id = ""

    source = os.getenv("PTI_SOURCE", "bot")
    strategy_id = os.getenv("PTI_STRATEGY_ID", "pti_1barup")
    urgency = (os.getenv("PTI_URGENCY", "normal") or "normal").strip().lower()

    stats_log_every = int(os.getenv("PTI_STATS_LOG_EVERY", "200"))

    log.info(
        COMPONENT,
        "startup.config",
        extra={
            "symbols_mode": symbols_mode,
            "symbols": symbols,
            "warm_bars_channel": warm_bars_channel,
            "up_mode": up_mode,
            "cooldown_sec": cooldown_sec,
            "intent_channel": intent_channel,
            "account_id": (account_id if account_id else None),
            "strategy_id": strategy_id,
            "source": source,
            "request_warm_tier": request_warm_tier,
            "warm_tier_name": warm_tier_name,
        },
    )

    return Config(
        redis_url=redis_url,
        symbols=symbols,
        symbols_mode=symbols_mode,
        active_set_key=active_set_key,
        filter_stream_channel=filter_stream_channel,
        warm_bars_channel=warm_bars_channel,
        up_mode=up_mode,
        cooldown_sec=cooldown_sec,
        stop_buffer=stop_buffer,
        request_warm_tier=request_warm_tier,
        warm_tier_name=warm_tier_name,
        intent_channel=intent_channel,
        account_id=account_id if account_id else None,
        source=source,
        strategy_id=strategy_id,
        urgency=urgency,
        stats_log_every=stats_log_every,
    )


async def request_tier(symbol: str, tier: str) -> None:
    try:
        evt = {"symbol": symbol, "tier": tier, "ts": time.time(), "source": COMPONENT}
        await publish_async(CHANNELS["raise"], evt)
        log.info(COMPONENT, "tier.requested", extra={"symbol": symbol, "tier": tier, "channel": CHANNELS["raise"]})
    except Exception as exc:
        log.exception(COMPONENT, "tier.request_failed", extra={"symbol": symbol, "tier": tier, "error": repr(exc)})


def _f(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None


def _iso_utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int((time.time() % 1) * 1000):03d}Z"


def _extract_bar_fields(bar: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[float], Optional[float], Optional[float], Optional[float]]:
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


def _strength_from_bar(o: float, c: float) -> float:
    if o <= 0:
        return 0.5
    body_pct = abs(c - o) / o
    s = body_pct / 0.005
    return 0.0 if s < 0 else 1.0 if s > 1.0 else s


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
        rx = msg.get("data")
        log.info(COMPONENT, "warm.rx", extra={"rx": rx, "active": len(active)})
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

        if kind == "add" and sym not in active:
            active.add(sym)
            log.info(COMPONENT, "filter.add", extra={"symbol": sym, "active_count": len(active)})
            if cfg.request_warm_tier:
                await request_tier(sym, cfg.warm_tier_name)

        elif kind == "remove" and sym in active:
            active.discard(sym)
            log.info(COMPONENT, "filter.remove", extra={"symbol": sym, "active_count": len(active)})


async def emit_intent(cfg: Config, sym: str, bar_ts: Optional[str], o: float, h: float, l: float, c: float, prev_close: Optional[float]) -> None:
    intent_id = str(uuid4())
    ts = _iso_utc_now()
    strength = _strength_from_bar(o, c)

    trigger: Dict[str, Any] = {"kind": "bar_close", "basis": "1m", "condition": "up", "up_mode": cfg.up_mode}
    if bar_ts is not None:
        trigger["bar_ts"] = bar_ts

    reason = "1barup: 1m bar closed up" if cfg.up_mode != "prevclose" else "1barup: 1m close > prev_close"

    invalidation = l
    initial_stop = max(0.0, l - cfg.stop_buffer)

    r = max(0.0, c - initial_stop)
    targets: List[Dict[str, Any]] = []
    if r > 0:
        targets = [{"kind": "r_multiple", "r": 1.0}, {"kind": "r_multiple", "r": 2.0}]

    intent: Dict[str, Any] = {
        "intent_id": intent_id,
        "ts": ts,
        "symbol": sym,
        "side": "buy",
        "source": cfg.source,
        "strategy_id": cfg.strategy_id,
        "account_id": cfg.account_id,
        "intent_key": f"{cfg.strategy_id}:{sym}:buy:bar_close:1m:{cfg.up_mode}",
        "strength": float(strength),
        "urgency": cfg.urgency,
        "trigger": trigger,
        "reason": reason,
        "tags": ["1barup", "1m", "bar_close", "up"],
        "context": {"timeframe": "1m"},
        "risk_hints": {
            "invalidation": float(invalidation),
            "initial_stop": float(initial_stop),
            "targets": targets,
            "notes": "Non-binding: stop anchored at signal bar low (+buffer).",
        },
        "diag": {"o": float(o), "h": float(h), "l": float(l), "c": float(c), "prev_close": float(prev_close) if prev_close is not None else None, "up_mode": cfg.up_mode},
    }


    await publish_async(cfg.intent_channel, {"intent": intent, "meta": {"model": "1barup"}})
    log.info(COMPONENT, "intent.publish_ok", extra={"intent_id": intent_id, "symbol": sym, "strength": strength, "channel": cfg.intent_channel, "account_id": cfg.account_id})


async def warm_bars_loop(cfg: Config, active: Set[str]) -> None:
    log.info(COMPONENT, "warm.loop_start", extra={"channel": cfg.warm_bars_channel})
    ps = await subscribe(cfg.warm_bars_channel)
    log.info(COMPONENT, "warm.subscribe_ok", extra={"channel": cfg.warm_bars_channel})

    last_close: Dict[str, float] = {}
    last_fire_ts: Dict[str, float] = {}

    rx = used = skipped = fires = 0

    async for msg in ps.listen():  # type: ignore[attr-defined]
        log.debug(COMPONENT, "warm.rx", extra={"msg": msg, "active": len(active)})
        if msg.get("type") != "message":
            continue
        rx += 1

        try:
            bar = unpack(msg["data"])
            log.debug(COMPONENT, "warm.bar_unpacked", extra={"bar": bar})
        except Exception:
            continue
        if not isinstance(bar, dict):
            continue
            
        sym, bar_ts, o, h, l, c = _extract_bar_fields(bar)
        if not sym or o is None or h is None or l is None or c is None:
            continue

        if sym not in active:
            skipped += 1
            continue

        used += 1

        prev = last_close.get(sym)
        last_close[sym] = c

        is_up = False
        if cfg.up_mode == "prevclose":
            if prev is not None and c > prev:
                is_up = True
        else:
            if c > o:
                is_up = True

        if is_up:
            now = time.time()
            last = last_fire_ts.get(sym, 0.0)
            if (now - last) >= cfg.cooldown_sec:
                last_fire_ts[sym] = now
                fires += 1
                await emit_intent(cfg, sym, bar_ts, o, h, l, c, prev)

        if rx % cfg.stats_log_every == 0:
            log.info(COMPONENT, "warm.stats", extra={"rx": rx, "used": used, "skipped": skipped, "active": len(active), "fires": fires})


async def run() -> None:
    cfg = load_config()

    if cfg.symbols_mode == "static":
        active: Set[str] = set(cfg.symbols)
        log.info(COMPONENT, "STATIC: active_set_initialized", extra={"active": list(active)})
        if cfg.request_warm_tier:
            for sym in sorted(active):
                await request_tier(sym, cfg.warm_tier_name)
        tasks = [asyncio.create_task(warm_bars_loop(cfg, active))]

    else:
        active = await bootstrap_active_set(cfg)
        log.info(COMPONENT, "DYNAMIC: active_set_initialized", extra={"active": list(active)})
        if cfg.request_warm_tier:
            for sym in sorted(active):
                await request_tier(sym, cfg.warm_tier_name)
        tasks = [
            asyncio.create_task(filter_stream_loop(cfg, active)),
            asyncio.create_task(warm_bars_loop(cfg, active)),
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
