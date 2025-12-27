from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import redis.asyncio as aioredis

from common import logging as log
from common.bus import CHANNELS, publish_async, subscribe, unpack

COMPONENT = "eval.pti_rbf"

# ---------------------------------------------------------------------------
# repo-root sys.path bootstrap (fixes common import when running as script)
# ---------------------------------------------------------------------------

_THIS = Path(__file__).resolve()
_repo_root = None
for p in [_THIS.parent, *_THIS.parents]:
    if (p / ".env").exists():
        _repo_root = p
        break
if _repo_root is None:
    _repo_root = _THIS.parents[2]
sys.path.insert(0, str(_repo_root))

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Config:
    redis_url: str
    active_set_key: str

    # Membership deltas
    filter_stream_channel: str

    # DataHub warm bars feed
    warm_bars_channel: str

    # DataHub cache window (bootstrap)
    cache_window_bars: int
    datahub_internal_url: Optional[str]

    # Pattern params
    min_leg_pct: float
    max_flag_retrace: float
    breakout_pct: float

    # Intent routing
    order_channel: str
    qty: int
    account_tag: str
    extended_hours: bool

    # Logging cadence
    stats_log_every: int


def load_config() -> Config:
    redis_url = (
        os.getenv("GARNET_URL")
        or os.getenv("REDIS_URL")
        or os.getenv("REFLEX_REDIS_URL")
        or "redis://127.0.0.1:6379/0"
    )

    active_set_key = os.getenv("ROSS_ACTIVE_KEY", "eval:fts_rbf:active")
    filter_stream_channel = os.getenv("ROSS_FILTER_STREAM_CHANNEL", "eval.ross_filter_stream")

    # IMPORTANT: this should be DataHub's WARM tier bar feed
    warm_bars_channel = os.getenv("RBF_WARM_BARS_CHANNEL", "hub.warm.bars1m")

    cache_window_bars = int(os.getenv("RBF_CACHE_WINDOW_BARS", "90"))
    datahub_internal_url = os.getenv("DATAHUB_API_BASE")  # e.g. http://127.0.0.1:8001

    min_leg_pct = float(os.getenv("ROSS_MIN_LEG_PCT", "0.02"))
    max_flag_retrace = float(os.getenv("ROSS_MAX_FLAG_RETRACE", "0.5"))
    breakout_pct = float(os.getenv("ROSS_BREAKOUT_PCT", "0.01"))

    order_channel = CHANNELS.get("order", "eval.order_intent")
    qty = int(os.getenv("ROSS_QTY", "100"))
    account_tag = os.getenv("ROSS_ACCOUNT_TAG", "margin").lower()
    extended_hours = os.getenv("ROSS_EXTENDED_HOURS", "0").lower() in ("1", "true", "yes", "on")

    stats_log_every = int(os.getenv("RBF_STATS_LOG_EVERY", "200"))

    return Config(
        redis_url=redis_url,
        active_set_key=active_set_key,
        filter_stream_channel=filter_stream_channel,
        warm_bars_channel=warm_bars_channel,
        cache_window_bars=cache_window_bars,
        datahub_internal_url=datahub_internal_url,
        min_leg_pct=min_leg_pct,
        max_flag_retrace=max_flag_retrace,
        breakout_pct=breakout_pct,
        order_channel=order_channel,
        qty=qty,
        account_tag=account_tag,
        extended_hours=extended_hours,
        stats_log_every=stats_log_every,
    )


# ---------------------------------------------------------------------------
# Pattern state machine
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

    def reset(self) -> None:
        self.state = FlagState.IDLE
        self.leg_start = None
        self.leg_high = None
        self.flag_low = None
        self.last_price = None

    def update(self, price: float, cfg: Config) -> Tuple[bool, FlagState, FlagState, Dict[str, Any]]:
        old = self.state
        now = time.time()
        self.last_price = price
        self.last_update = now

        fired = False

        if self.state == FlagState.IDLE:
            if self.leg_start is None:
                self.leg_start = price
                self.leg_high = price

            move = (price - self.leg_start) / self.leg_start if self.leg_start else 0.0
            if move >= cfg.min_leg_pct:
                self.state = FlagState.LEG_UP
                self.leg_high = max(self.leg_high or price, price)

        elif self.state == FlagState.LEG_UP:
            self.leg_high = max(self.leg_high or price, price)
            if self.leg_high and price < self.leg_high:
                self.state = FlagState.FLAG
                self.flag_low = price

        elif self.state == FlagState.FLAG:
            if self.leg_high is None:
                self.reset()
            else:
                self.flag_low = min(self.flag_low or price, price)
                retrace = (self.leg_high - (self.flag_low or price)) / self.leg_high if self.leg_high else 0.0

                if retrace > cfg.max_flag_retrace:
                    self.state = FlagState.BROKEN

                breakout_level = self.leg_high * (1.0 + cfg.breakout_pct)
                if price >= breakout_level:
                    fired = True
                    # keep it simple for now; tomorrow we can add cooldown
                    self.reset()

        # BROKEN: do nothing until reset externally (optional)
        diag = {
            "symbol": self.symbol,
            "state": self.state.value,
            "old_state": old.value,
            "leg_start": self.leg_start,
            "leg_high": self.leg_high,
            "flag_low": self.flag_low,
            "last_price": self.last_price,
            "ts": now,
        }
        return fired, old, self.state, diag


# ---------------------------------------------------------------------------
# DataHub cache adapter (edit here if needed)
# ---------------------------------------------------------------------------

async def fetch_recent_bars_from_datahub(cfg: Config, symbol: str, limit: int) -> List[Dict[str, Any]]:
    """
    Fetch last N 1m bars from DataHub internal API.

    Uses DataHub's Flask endpoint:
      GET /v1/history/bars1m?symbol=XYZ&limit=90

    Response contains bars shaped like:
      { "ts": <ns>, "close": <float>, "volume": <int> }
    """
    if not cfg.datahub_internal_url:
        log.info(COMPONENT, "cache.disabled_no_datahub_url", extra={"symbol": symbol})
        return []

    import urllib.parse
    import urllib.request

    base = cfg.datahub_internal_url.rstrip("/")
    qs = urllib.parse.urlencode({"symbol": symbol.upper(), "limit": int(limit)})
    url = f"{base}/v1/history/bars1m?{qs}"

    def _do() -> List[Dict[str, Any]]:
        try:
            with urllib.request.urlopen(url, timeout=2.5) as resp:
                raw = resp.read()
            obj = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            return [{"__error__": repr(exc)}]

        if not isinstance(obj, dict) or not obj.get("ok"):
            return [{"__error__": f"bad_response ok={obj.get('ok') if isinstance(obj, dict) else None}"}]

        bars = obj.get("bars") or []
        if not isinstance(bars, list):
            return [{"__error__": "bars_not_list"}]

        # Keep bars in the returned shape; extract_close() already supports close key.
        return [b for b in bars if isinstance(b, dict)]

    bars = await asyncio.to_thread(_do)

    if bars and "__error__" in bars[0]:
        log.info(COMPONENT, "cache.fetch_failed", extra={"symbol": symbol, "url": url, "error": bars[0]["__error__"]})
        return []

    log.info(COMPONENT, "cache.fetch_ok", extra={"symbol": symbol, "url": url, "count": len(bars)})
    return bars



def extract_close(bar: Dict[str, Any]) -> Optional[float]:
    raw = bar.get("c") or bar.get("close")
    if raw is None:
        return None
    try:
        return float(raw)
    except Exception:
        return None


async def initialize_symbol_state(cfg: Config, states: Dict[str, RossPatternState], symbol: str) -> bool:
    bars = await fetch_recent_bars_from_datahub(cfg, symbol, cfg.cache_window_bars)
    if not bars:
        log.info(COMPONENT, "state.init.missing_cache", extra={"symbol": symbol})
        return False

    st = states.get(symbol)
    if st is None:
        st = RossPatternState(symbol=symbol)
        states[symbol] = st
    else:
        st.reset()

    used = 0
    for bar in bars:
        c = extract_close(bar)
        if c is None:
            continue
        used += 1
        st.update(c, cfg)

    log.info(
        COMPONENT,
        "state.init",
        extra={"symbol": symbol, "bars_used": used, "state": st.state.value},
    )
    return used > 0


# ---------------------------------------------------------------------------
# Membership management
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


async def filter_stream_loop(cfg: Config, active: Set[str], newly_added: asyncio.Queue[str]) -> None:
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
                await newly_added.put(sym)
                log.info(COMPONENT, "filter.add", extra={"symbol": sym, "active_count": len(active)})
        elif kind == "remove":
            if sym in active:
                active.discard(sym)
                log.info(COMPONENT, "filter.remove", extra={"symbol": sym, "active_count": len(active)})


# ---------------------------------------------------------------------------
# Warm bars processing (DataHub)
# ---------------------------------------------------------------------------

async def warm_bars_loop(cfg: Config, active: Set[str], states: Dict[str, RossPatternState], initialized: Set[str]) -> None:
    ps = await subscribe(cfg.warm_bars_channel)
    log.info(COMPONENT, "warm.subscribe_ok", extra={"channel": cfg.warm_bars_channel})

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

        sym = (bar.get("sym") or bar.get("symbol") or "")
        if not sym:
            continue
        sym = str(sym).upper()

        if sym not in active:
            skipped += 1
            continue

        # If not initialized, we still proceed with live updates, but we log the mismatch.
        if sym not in initialized:
            log.info(COMPONENT, "state.uninitialized_live_update", extra={"symbol": sym})
            initialized.add(sym)

        c = extract_close(bar)
        if c is None:
            continue

        st = states.get(sym)
        if st is None:
            st = RossPatternState(symbol=sym)
            states[sym] = st

        fired, old, new, diag = st.update(c, cfg)
        used += 1

        if old != new:
            log.info(COMPONENT, "state.transition", extra={"symbol": sym, "from": old.value, "to": new.value})

        if fired:
            fires += 1
            intent = {
                "symbol": sym,
                "side": "buy",
                "qty": cfg.qty,
                "type": "market",
                "time_in_force": "day",
                "account_tag": cfg.account_tag,
                "extended_hours": cfg.extended_hours,
                "note": "ross_bullflag breakout",
            }
            envelope = {"intent": intent, "meta": {"model": "ross_bullflag", "diag": diag}}
            await publish_async(cfg.order_channel, envelope)
            log.info(COMPONENT, "intent.fire", extra={"symbol": sym, "price": c})

        if rx % cfg.stats_log_every == 0:
            log.info(
                COMPONENT,
                "warm.stats",
                extra={
                    "rx": rx,
                    "used": used,
                    "skipped": skipped,
                    "active": len(active),
                    "states": len(states),
                    "fires": fires,
                },
            )


async def initializer_loop(cfg: Config, states: Dict[str, RossPatternState], initialized: Set[str], newly_added: asyncio.Queue[str]) -> None:
    while True:
        sym = await newly_added.get()
        if sym in initialized:
            continue
        ok = await initialize_symbol_state(cfg, states, sym)
        if ok:
            initialized.add(sym)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run() -> None:
    cfg = load_config()
    log.info(
        COMPONENT,
        "startup.config",
        extra={
            "active_set_key": cfg.active_set_key,
            "filter_stream_channel": cfg.filter_stream_channel,
            "warm_bars_channel": cfg.warm_bars_channel,
            "cache_window_bars": cfg.cache_window_bars,
            "datahub_internal_url": cfg.datahub_internal_url,
        },
    )

    active = await bootstrap_active_set(cfg)
    states: Dict[str, RossPatternState] = {}
    initialized: Set[str] = set()

    newly_added: asyncio.Queue[str] = asyncio.Queue()

    # Initialize existing active symbols (best-effort)
    for sym in sorted(active):
        await newly_added.put(sym)

    tasks = [
        asyncio.create_task(filter_stream_loop(cfg, active, newly_added)),
        asyncio.create_task(initializer_loop(cfg, states, initialized, newly_added)),
        asyncio.create_task(warm_bars_loop(cfg, active, states, initialized)),
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    for t in pending:
        t.cancel()
    for t in done:
        exc = t.exception()
        if exc:
            raise exc


if __name__ == "__main__":
    if os.name == "nt":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
        except Exception:
            pass
    asyncio.run(run())
