from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Set, Tuple, List


import psycopg
import psycopg.rows
import psycopg
import psycopg.rows
import redis.asyncio as aioredis

# -----------------------------------------------------------------------------
# Absolute repo-root sys.path anchor
# <repo>/evaluator/bots/FTS_rbf.py -> parents[2] == <repo>
# -----------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# -----------------------------------------------------------------------------
# Structured logger
# -----------------------------------------------------------------------------
COMPONENT = "eval.ross_filter_stream"

class _JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = {
            "ts": time.time(),
            "level": record.levelname,
            "instance": os.getenv("REFLEX_INSTANCE_ID", "default"),
            "mode": os.getenv("REFLEX_MODE", "LIVE"),
            "component": COMPONENT,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            base["extra"] = {"extra": extra}
        return json.dumps(base, ensure_ascii=False)

_log = logging.getLogger("fts_rbf")
if not _log.handlers:
    _log.setLevel(logging.INFO)
    h = logging.StreamHandler()
    h.setFormatter(_JsonLogFormatter())
    _log.addHandler(h)
    _log.propagate = False

def _info(msg: str, extra: Optional[Dict[str, Any]] = None) -> None:
    _log.info(msg, extra={"extra": extra or {}})

def _warn(msg: str, extra: Optional[Dict[str, Any]] = None) -> None:
    _log.warning(msg, extra={"extra": extra or {}})

def _error(msg: str, extra: Optional[Dict[str, Any]] = None) -> None:
    _log.error(msg, extra={"extra": extra or {}})

# -----------------------------------------------------------------------------
# Deterministic --env loader (loads before reading env vars)
# -----------------------------------------------------------------------------
def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=None, help="Path to .env")
    return ap.parse_args()

def _load_dotenv_file(path: Path) -> Tuple[bool, int]:
    """
    Loads KEY=VALUE lines into os.environ, without overriding existing vars.
    Returns (loaded_ok, lines_loaded).
    """
    if not path.exists():
        return False, 0
    loaded = 0
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if not k:
                continue
            if k not in os.environ:
                os.environ[k] = v
                loaded += 1
        return True, loaded
    except Exception:
        return False, loaded

_args = _parse_args()
_env_path = Path(_args.env).resolve() if _args.env else (_REPO_ROOT / ".env")
_env_ok, _env_loaded = _load_dotenv_file(_env_path)

_info(
    "argv.debug",
    extra={
        "argv": sys.argv,
        "env_arg": _args.env,
        "env_path": str(_env_path),
        "env_exists": _env_path.exists(),
        "env_loaded": _env_loaded,
    },
)
_info(
    "env.debug",
    extra={
        "REFLEX_INSTANCE_ID": os.getenv("REFLEX_INSTANCE_ID"),
        "REFLEX_MODE": os.getenv("REFLEX_MODE"),
        "REFLEX_RUN_ID": os.getenv("REFLEX_RUN_ID"),
        "REFLEX_PG_DSN_set": bool(os.getenv("REFLEX_PG_DSN") or os.getenv("REFLEX__PG_DSN")),
    },
)

# -----------------------------------------------------------------------------
# Imports that depend on sys.path/env being correct
# -----------------------------------------------------------------------------
from common.bus import CHANNELS, subscribe, unpack, publish_async  # noqa: E402


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    redis_url: str
    pg_dsn: str

    bars_channel: str
    filter_stream_channel: str
    raise_channel: str

    universe_key: str
    active_key: str

    # Durable change stream (Redis Streams)
    filter_stream_key: str
    filter_stream_maxlen: int
    filter_stream_reset: bool
    publish_pubsub: bool

    # Pool sizing & timing
    pool_refresh_sec: float
    warm_total: int
    warm_scouts: int

    # Filter knobs (stage-2)
    price_band: Sequence[float]
    vol_bar_min: float

    # Logging
    heartbeat_sec: float

    # Tier publish throttle
    tier_publish_sleep_s: float


def load_config() -> Config:
    redis_url = os.getenv("GARNET_URL") or os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0"
    pg_dsn = os.getenv("REFLEX_PG_DSN") or os.getenv("REFLEX__PG_DSN") or ""
    if not pg_dsn:
        _error("config.pg_dsn_missing")

    bars_channel = os.getenv("FTS_RBF_BARS_CHANNEL", "hub.bars1m")
    filter_stream_channel = os.getenv("FTS_RBF_FILTER_CHANNEL", "eval.fts_rbf.stream")
    raise_channel = os.getenv("EVAL_RAISE_CHANNEL", CHANNELS["raise"])

    universe_key = os.getenv("FTS_RBF_UNIVERSE_KEY", "eval:fts_rbf:universe")
    active_key = os.getenv("FTS_RBF_ACTIVE_KEY", "eval:fts_rbf:active")

    filter_stream_key = os.getenv("FTS_RBF_STREAM_KEY", "eval:fts_rbf:stream")
    filter_stream_maxlen = int(os.getenv("FTS_RBF_STREAM_MAXLEN", "20000"))
    filter_stream_reset = os.getenv("FTS_RBF_STREAM_RESET", "1").strip() not in ("0","false","False","no","NO")
    publish_pubsub = os.getenv("FTS_RBF_PUBSUB_ENABLE", "1").strip() not in ("0","false","False","no","NO")

    pool_refresh_sec = float(os.getenv("FTS_RBF_POOL_REFRESH_SEC", "60"))
    warm_total = int(os.getenv("FTS_RBF_WARM_TOTAL", "60"))          # e.g. 50 keepers + 10 scouts
    warm_scouts = int(os.getenv("FTS_RBF_WARM_SCOUTS", "10"))

    price_min = float(os.getenv("FTS_RBF_PRICE_MIN", "1"))
    price_max = float(os.getenv("FTS_RBF_PRICE_MAX", "20"))
    vol_bar_min = float(os.getenv("FTS_RBF_VOL_BAR_MIN", "7500"))

    # Stage-1 screener
    FTS_RBF_PRICE_MIN=1
    FTS_RBF_PRICE_MAX=20

    FTS_RBF_FLOAT_ALLOW_NULL=1
    FTS_RBF_FLOAT_MIN=1000000
    FTS_RBF_FLOAT_MAX=20000000


    heartbeat_sec = float(os.getenv("FTS_RBF_HEARTBEAT_SEC", "30"))
    tier_publish_sleep_s = float(os.getenv("FTS_RBF_TIER_PUBLISH_SLEEP_S", "0.002"))

    # sanity
    if warm_total < 1:
        warm_total = 1
    if warm_scouts < 0:
        warm_scouts = 0
    if warm_scouts >= warm_total:
        warm_scouts = max(0, warm_total - 1)

    return Config(
        redis_url=redis_url,
        pg_dsn=pg_dsn,
        bars_channel=bars_channel,
        filter_stream_channel=filter_stream_channel,
        raise_channel=raise_channel,
        universe_key=universe_key,
        active_key=active_key,
        filter_stream_key=filter_stream_key,
        filter_stream_maxlen=filter_stream_maxlen,
        filter_stream_reset=filter_stream_reset,
        publish_pubsub=publish_pubsub,
        pool_refresh_sec=pool_refresh_sec,
        warm_total=warm_total,
        warm_scouts=warm_scouts,
        price_band=(price_min, price_max),
        vol_bar_min=vol_bar_min,
        heartbeat_sec=heartbeat_sec,
        tier_publish_sleep_s=tier_publish_sleep_s,
    )


# -----------------------------------------------------------------------------
# Stage 1 bootstrap
# -----------------------------------------------------------------------------





def _load_universe_from_pg(pg_dsn: str) -> Set[str]:
    if not pg_dsn:
        return set()

    # Stage-1 screen params (env-driven)
    price_min = float(os.getenv("FTS_RBF_PRICE_MIN", "1"))
    price_max = float(os.getenv("FTS_RBF_PRICE_MAX", "20"))

    float_allow_null = os.getenv("FTS_RBF_FLOAT_ALLOW_NULL", "1").strip().lower() not in ("0", "false", "no")
    float_min = int(os.getenv("FTS_RBF_FLOAT_MIN", "1000000"))
    float_max = int(os.getenv("FTS_RBF_FLOAT_MAX", "20000000"))

    # Use latest available DAILY close as the stable price proxy.
    # (This is "last completed daily bar", typically yesterday during market hours.)
    sql = """
        SELECT sp.symbol
        FROM symbol_profile_view sp
        JOIN LATERAL (
            SELECT d.close AS px_close
            FROM daily_bars d
            WHERE d.symbol = sp.symbol
              AND d.close IS NOT NULL
            ORDER BY d.timestamp DESC
            LIMIT 1
        ) d1 ON TRUE
        WHERE
          d1.px_close BETWEEN %s AND %s
          AND (
                (%s = TRUE AND sp.shares_float IS NULL)
             OR (sp.shares_float BETWEEN %s AND %s)
          )
    """

    syms: Set[str] = set()
    with psycopg.connect(pg_dsn, row_factory=psycopg.rows.dict_row) as conn:  # type: ignore[arg-type]
        with conn.cursor() as cur:
            cur.execute(sql, (price_min, price_max, float_allow_null, float_min, float_max))
            for row in cur:
                s = (row.get("symbol") or "").strip().upper()
                if s:
                    syms.add(s)
    return syms



async def bootstrap_universe(cfg: Config) -> Set[str]:
    """
    Bootstrap the trading universe by loading symbols from PostgreSQL and storing them in Redis.

    This function performs the following operations:
    1. Loads universe symbols from a PostgreSQL database using the provided connection string
    2. Clears any existing universe data in Redis
    3. Populates Redis with the loaded symbols as a sorted set
    4. Handles errors gracefully and logs appropriate messages throughout the process

    Args:
        cfg (Config): Configuration object containing:
            - pg_dsn: PostgreSQL connection string for loading universe data
            - redis_url: Redis connection URL for storing universe data  
            - universe_key: Redis key name where universe symbols will be stored

    Returns:
        Set[str]: A set of symbol strings representing the trading universe.
                  Returns an empty set if loading fails or no symbols are found.

    Raises:
        The function handles all exceptions internally and returns an empty set
        on failure, while logging appropriate error messages.

    Note:
        This function uses asyncio.to_thread() to run the PostgreSQL query
        in a thread pool to avoid blocking the event loop.
    """
    print("\n***Bootstrapping universe...***")
    _info("bootstrap.pg_query_begin")
    if not cfg.pg_dsn:
        _error("bootstrap.pg_dsn_missing")
        return set()

    try:
        universe = await asyncio.to_thread(_load_universe_from_pg, cfg.pg_dsn)
    except Exception as exc:
        _error("bootstrap.pg_query_failed", extra={"error": repr(exc)})
        return set()

    _info("bootstrap.pg_candidates_loaded", extra={"count": len(universe)})

    r = aioredis.from_url(cfg.redis_url, decode_responses=True)
    try:
        await r.delete(cfg.universe_key)
        if universe:
            await r.sadd(cfg.universe_key, *sorted(universe))
        _info("bootstrap.redis_sets_populated", extra={"universe_key": cfg.universe_key, "count": len(universe)})
    finally:
        try:
            await r.aclose()
        except Exception:
            pass

    if not universe:
        _error("bootstrap.empty_universe")
    print(f"Bootstrapped universe with {len(universe)} symbols.")
    return universe


# -----------------------------------------------------------------------------
# Tier control helpers (IMPORTANT: per-symbol messages; DataHub expects this)
# -----------------------------------------------------------------------------
async def raise_tier(cfg: Config, symbol: str, tier: str) -> None:
    evt = {"symbol": symbol, "tier": tier, "ts": time.time(), "source": COMPONENT}
    await publish_async(cfg.raise_channel, evt)

async def raise_many(cfg: Config, symbols: Sequence[str], tier: str) -> int:
    sent = 0
    for sym in symbols:
        try:
            await raise_tier(cfg, sym, tier)
            sent += 1
        except Exception as exc:
            _warn("tier.publish_failed", extra={"symbol": sym, "tier": tier, "error": repr(exc)})
        if cfg.tier_publish_sleep_s > 0:
            await asyncio.sleep(cfg.tier_publish_sleep_s)
    return sent


# -----------------------------------------------------------------------------
# Filter (stage-2) utilities
# -----------------------------------------------------------------------------
def _get_close(bar: Dict[str, Any]) -> Optional[float]:
    c = bar.get("c") or bar.get("close")
    if c is None:
        return None
    try:
        return float(c)
    except Exception:
        return None

def _get_vol(bar: Dict[str, Any]) -> float:
    v = bar.get("v") or bar.get("volume")
    try:
        return float(v) if v is not None else 0.0
    except Exception:
        return 0.0

def stage2_passes(cfg: Config, bar: Dict[str, Any]) -> bool:
    c = _get_close(bar)
    if c is None:
        return False
    v = _get_vol(bar)

    pmin, pmax = cfg.price_band
    if not (pmin <= c <= pmax):
        return False
    if cfg.vol_bar_min > 0 and v < cfg.vol_bar_min:
        return False
    return True


# -----------------------------------------------------------------------------
# Warm pool manager (keepers + scouts)
# -----------------------------------------------------------------------------
class WarmPool:
    def __init__(self, universe_sorted: List[str], warm_total: int, warm_scouts: int) -> None:
        self.universe_sorted = universe_sorted
        self.warm_total = warm_total
        self.warm_scouts = warm_scouts
        self.warm_keepers = max(0, warm_total - warm_scouts)

        self.scout_cursor = 0
        self.current_warm: Set[str] = set()

        # Activity score we learn ONLY when symbol is warm (from bars)
        self.score: Dict[str, float] = {}
        self.last_seen_ts: Dict[str, float] = {}

    def observe_bar(self, sym: str, bar_vol: float) -> None:
        # simple score: exponential-ish decay + add volume
        prev = self.score.get(sym, 0.0)
        new_score = prev * 0.85 + bar_vol
        self.score[sym] = new_score
        self.last_seen_ts[sym] = time.time()

    def _pick_scouts(self, forbidden: Set[str]) -> List[str]:
        scouts: List[str] = []
        n = len(self.universe_sorted)
        if n == 0 or self.warm_scouts <= 0:
            return scouts

        # round-robin through universe
        attempts = 0
        while len(scouts) < self.warm_scouts and attempts < n * 2:
            sym = self.universe_sorted[self.scout_cursor % n]
            self.scout_cursor += 1
            attempts += 1
            if sym in forbidden:
                continue
            scouts.append(sym)
            forbidden.add(sym)
        return scouts

    def compute_desired_warm(self) -> Set[str]:
        # Keepers: top by score (only among those with some score)
        scored = [(s, self.score.get(s, 0.0)) for s in self.universe_sorted]
        scored.sort(key=lambda x: x[1], reverse=True)

        keepers: List[str] = []
        for sym, sc in scored:
            if sc <= 0:
                continue
            keepers.append(sym)
            if len(keepers) >= self.warm_keepers:
                break

        # If we don't have enough scored keepers yet, fill deterministically from alphabet
        if len(keepers) < self.warm_keepers:
            for sym in self.universe_sorted:
                if sym in keepers:
                    continue
                keepers.append(sym)
                if len(keepers) >= self.warm_keepers:
                    break

        forbidden = set(keepers)
        scouts = self._pick_scouts(forbidden)
        desired = set(keepers + scouts)
        return desired


# -----------------------------------------------------------------------------
# Main loop: subscribe bars, update scores, manage pool + active stream
# -----------------------------------------------------------------------------
async def run(cfg: Config) -> None:
    universe = await bootstrap_universe(cfg)
    universe_sorted = sorted(universe)

    _info("startup", extra={
        "bars_channel": cfg.bars_channel,
        "filter_stream_channel": cfg.filter_stream_channel,
        "filter_stream_key": cfg.filter_stream_key,
        "publish_pubsub": cfg.publish_pubsub,
        "stream_reset": cfg.filter_stream_reset,
        "raise_channel": cfg.raise_channel,
        "redis_url": cfg.redis_url,
        "pg_dsn_set": bool(cfg.pg_dsn),
        "universe": len(universe),
        "pool_refresh_sec": cfg.pool_refresh_sec,
        "warm_total": cfg.warm_total,
        "warm_scouts": cfg.warm_scouts,
    })

    if not universe:
        _error("fatal.empty_universe")
        return

    # 1) Raise ALL to WATCH (membership)
    sent_watch = await raise_many(cfg, universe_sorted, "WATCH")
    _info("bootstrap.watch.requested", extra={"count": len(universe_sorted), "sent": sent_watch})

    pool = WarmPool(universe_sorted, cfg.warm_total, cfg.warm_scouts)

    # 2) Connect redis for active set management
    r = aioredis.from_url(cfg.redis_url, decode_responses=True)
    # Fresh run defaults: clear active set; optionally clear the change stream.
    await r.delete(cfg.active_key)
    if cfg.filter_stream_reset:
        try:
            await r.delete(cfg.filter_stream_key)
        except Exception as exc:
            _warn("stream.reset_failed", extra={"key": cfg.filter_stream_key, "error": repr(exc)})

    # 3) Subscribe to bars stream
    ps = await subscribe(cfg.bars_channel)
    _info("bars.subscribe_ok", extra={"channel": cfg.bars_channel})

    # state
    active: Set[str] = set()
    msgs_seen = 0
    bars_seen = 0
    last_bar_ts: Optional[float] = None
    adds_active = 0
    rems_active = 0
    warm_adds = 0
    warm_rems = 0

    async def heartbeat():
        while True:
            await asyncio.sleep(cfg.heartbeat_sec)
            age = round(time.time() - last_bar_ts, 1) if last_bar_ts else None
            _info("heartbeat", extra={
                "universe": len(universe),
                "warm": len(pool.current_warm),
                "active": len(active),
                "msgs_seen": msgs_seen,
                "bars_seen": bars_seen,
                "warm_adds": warm_adds,
                "warm_rems": warm_rems,
                "active_adds": adds_active,
                "active_rems": rems_active,
                "last_bar_age_s": age,
            })

    async def refresh_pool():
        nonlocal warm_adds, warm_rems
        while True:
            await asyncio.sleep(cfg.pool_refresh_sec)

            desired = pool.compute_desired_warm()
            to_warm = sorted(desired - pool.current_warm)
            to_watch = sorted(pool.current_warm - desired)

            if to_warm:
                n = await raise_many(cfg, to_warm, "WARM")
                warm_adds += n
            if to_watch:
                n = await raise_many(cfg, to_watch, "WATCH")
                warm_rems += n

            pool.current_warm = desired

            _info("pool.refresh", extra={
                "desired_warm": len(desired),
                "to_warm": len(to_warm),
                "to_watch": len(to_watch),
                "warm_total": cfg.warm_total,
                "warm_scouts": cfg.warm_scouts,
            })

    hb_task = asyncio.create_task(heartbeat())
    pool_task = asyncio.create_task(refresh_pool())

    # Prime pool immediately (no waiting 60s)
    desired0 = pool.compute_desired_warm()
    to_warm0 = sorted(desired0 - pool.current_warm)
    if to_warm0:
        n = await raise_many(cfg, to_warm0, "WARM")
        warm_adds += n
    pool.current_warm = desired0
    _info("pool.prime", extra={"warm": len(pool.current_warm), "to_warm": len(to_warm0)})

    async def emit_change(kind: str, symbol: str) -> None:
        """Emit stage-2 membership changes in TWO ways:
        1) Pub/Sub (fast, but not durable)
        2) Durable log (Redis List) for catch-up
        """
        evt: Dict[str, Any] = {
            "kind": kind,
            "symbol": symbol,
            "ts": time.time(),
            "source": COMPONENT,
            "instance": os.getenv("REFLEX_INSTANCE_ID", ""),
            "run_id": os.getenv("REFLEX_RUN_ID", ""),
        }

        # Pub/Sub
        if cfg.publish_pubsub:
            try:
                await publish_async(cfg.filter_stream_channel, evt)
            except Exception as exc:
                _warn(
                    "stream.pubsub_publish_failed",
                    extra={"error": repr(exc), "symbol": symbol, "kind": kind},
                )

        # Durable log: Redis List (works on Redis and Garnet)
        try:
            entry = json.dumps(evt, separators=(",", ":"), default=str)
            await r.rpush(cfg.filter_stream_key, entry)
            if cfg.filter_stream_maxlen and cfg.filter_stream_maxlen > 0:
                await r.ltrim(cfg.filter_stream_key, -cfg.filter_stream_maxlen, -1)
        except Exception as exc:
            _warn(
                "durable_log.rpush_failed",
                extra={"error": repr(exc), "key": cfg.filter_stream_key, "symbol": symbol, "kind": kind},
            )

    # ---- MAIN BAR CONSUME LOOP (this was accidentally outside run) ----
    try:
        async for msg in ps.listen():  # type: ignore[attr-defined]
            if msg.get("type") != "message":
                continue
            msgs_seen += 1

            try:
                bar = unpack(msg["data"])
            except Exception as exc:
                _warn("bars.unpack_error", extra={"error": repr(exc)})
                continue
            if not isinstance(bar, dict):
                continue

            sym = (bar.get("sym") or bar.get("symbol") or "")
            sym = str(sym).upper() if sym else ""
            if not sym:
                continue

            # Only compute for current warm pool
            if sym not in pool.current_warm:
                continue

            bars_seen += 1
            last_bar_ts = time.time()

            # Observe activity score
            pool.observe_bar(sym, _get_vol(bar))

            # Stage-2 filter: keep active list
            ok = stage2_passes(cfg, bar)
            if ok and sym not in active:
                active.add(sym)
                adds_active += 1
                await r.sadd(cfg.active_key, sym)
                await emit_change("add", sym)
                _info("filter.add", extra={"symbol": sym, "active": len(active)})

            elif (not ok) and sym in active:
                active.remove(sym)
                rems_active += 1
                await r.srem(cfg.active_key, sym)
                await emit_change("remove", sym)
                _info("filter.remove", extra={"symbol": sym, "active": len(active)})

    except KeyboardInterrupt:
        _info("shutdown.keyboard_interrupt")
    finally:
        hb_task.cancel()
        pool_task.cancel()
        try:
            await r.aclose()
        except Exception:
            pass



def main() -> None:
    cfg = load_config()
    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()