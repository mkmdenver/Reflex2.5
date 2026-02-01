# evaluator/bots/FTS_1barup.py
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

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


# NOTE: .env loading is handled by the launcher BAT (env.bat).
from common import logging as log
from common.bus import CHANNELS, publish_async

COMPONENT = "eval.fts_1barup"


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
    pg_dsn: str

    mode: str  # "static" | "dbscan"
    static_symbols: List[str]

    # Dedicated 1BarUp membership feed
    active_key: str
    filter_stream_channel: str

    # DataHub tier control
    raise_channel: str
    raise_tier: str
    raise_enable: bool
    tier_publish_sleep_s: float

    # DB scan params
    price_max: float
    float_allow_null: bool
    float_min: int
    float_max: int

    # Refresh behavior (dbscan only)
    refresh_sec: float

    # Logging cadence
    heartbeat_sec: float


def load_config() -> Config:
    redis_url = (
        os.getenv("GARNET_URL")
        or os.getenv("REDIS_URL")
        or os.getenv("REFLEX_REDIS_URL")
        or "redis://127.0.0.1:6379/0"
    )

    pg_dsn = (
        os.getenv("REFLEX_PG_DSN")
        or os.getenv("PG_DSN")
        or os.getenv("DATABASE_URL")
        or ""
    )

    mode = (os.getenv("FTS_1BAR_MODE", "static") or "static").strip().lower()
    if mode not in ("static", "dbscan"):
        mode = "static"

    static_symbols = _csv_syms(os.getenv("FTS_1BAR_SYMBOLS", ""))

    # Dedicated feed defaults (so we don't collide with RBF)
    active_key = os.getenv("FTS_1BAR_ACTIVE_KEY", "eval:fts_1barup:active")
    filter_stream_channel = os.getenv("FTS_1BAR_FILTER_STREAM_CHANNEL", "eval.1barup_filter_stream")

    # DataHub tier control (WATCH is enough to get bars)
    raise_channel = (
        os.getenv("TIERS_CMD_Q")
        or os.getenv("REFLEX_RAISE_CHANNEL")
        or os.getenv("FTS_1BAR_RAISE_CHANNEL")
        or CHANNELS.get("raise", "hub.tier.raise")
    )
    raise_tier = (os.getenv("FTS_1BAR_RAISE_TIER", "WATCH") or "WATCH").strip().upper()
    raise_enable = (os.getenv("FTS_1BAR_RAISE_ENABLE", "1") or "1").strip().lower() not in ("0", "false", "no", "off")
    tier_publish_sleep_s = float(os.getenv("FTS_1BAR_TIER_PUBLISH_SLEEP_S", "0.002"))

    # DB scan params
    price_max = float(os.getenv("FTS_1BAR_PRICE_MAX", "20"))

    float_allow_null = (os.getenv("FTS_1BAR_FLOAT_ALLOW_NULL", "1") or "1").strip().lower() not in ("0", "false", "no", "off")
    float_min = int(os.getenv("FTS_1BAR_FLOAT_MIN", "1000000"))
    float_max = int(os.getenv("FTS_1BAR_FLOAT_MAX", "20000000"))

    refresh_sec = float(os.getenv("FTS_1BAR_REFRESH_SEC", "300"))
    heartbeat_sec = float(os.getenv("FTS_1BAR_HEARTBEAT_SEC", "30"))

    return Config(
        redis_url=redis_url,
        pg_dsn=pg_dsn,
        mode=mode,
        static_symbols=static_symbols,
        active_key=active_key,
        filter_stream_channel=filter_stream_channel,
        raise_channel=raise_channel,
        raise_tier=raise_tier,
        raise_enable=raise_enable,
        tier_publish_sleep_s=tier_publish_sleep_s,
        price_max=price_max,
        float_allow_null=float_allow_null,
        float_min=float_min,
        float_max=float_max,
        refresh_sec=refresh_sec,
        heartbeat_sec=heartbeat_sec,
    )


async def _raise_tier(symbol: str, tier: str, raise_channel: str) -> None:
    evt = {"symbol": symbol, "tier": tier, "ts": time.time(), "source": COMPONENT}
    await publish_async(raise_channel, evt)


def _load_dbscan_universe(pg_dsn: str, price_max: float, float_allow_null: bool, float_min: int, float_max: int) -> Set[str]:
    """
    DBSCAN:
      - latest daily close < price_max
      - shares_float between [float_min, float_max] (or NULL if allowed)
    Uses daily_bars latest close as the "last price recorded in daily".
    """
    if not pg_dsn:
        return set()

    try:
        import psycopg
        import psycopg.rows
    except Exception:
        # psycopg not installed / import issue
        return set()

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
          d1.px_close < %s
          AND (
                (%s = TRUE AND sp.shares_float IS NULL)
             OR (sp.shares_float BETWEEN %s AND %s)
          )
    """

    out: Set[str] = set()
    with psycopg.connect(pg_dsn, row_factory=psycopg.rows.dict_row) as conn:  # type: ignore[arg-type]
        with conn.cursor() as cur:
            cur.execute(sql, (price_max, float_allow_null, float_min, float_max))
            for row in cur:
                s = (row.get("symbol") or "").strip().upper()
                if s:
                    out.add(s)
    return out


async def _emit_membership_deltas(
    cfg: Config,
    r: aioredis.Redis,
    new_set: Set[str],
    old_set: Set[str],
) -> Tuple[int, int]:
    adds = sorted(new_set - old_set)
    rems = sorted(old_set - new_set)

    # Apply to Redis set first (source of truth)
    if adds:
        await r.sadd(cfg.active_key, *adds)
    if rems:
        await r.srem(cfg.active_key, *rems)

    # Publish deltas
    async def pub(kind: str, sym: str) -> None:
        evt = {"kind": kind, "symbol": sym, "ts": time.time(), "source": COMPONENT}
        await publish_async(cfg.filter_stream_channel, evt)

    for sym in adds:
        await pub("add", sym)
    for sym in rems:
        await pub("remove", sym)

    return len(adds), len(rems)


async def _heartbeat_loop(cfg: Config, state: Dict[str, Any]) -> None:
    while True:
        await asyncio.sleep(cfg.heartbeat_sec)
        log.info(
            COMPONENT,
            "heartbeat",
            extra={
                "mode": cfg.mode,
                "active_key": cfg.active_key,
                "filter_stream_channel": cfg.filter_stream_channel,
                "active_count": state.get("active_count", 0),
                "last_refresh_ago_s": (time.time() - float(state.get("last_refresh_ts", 0.0))) if state.get("last_refresh_ts") else None,
            },
        )


async def run() -> None:
    cfg = load_config()
    log.info(
        COMPONENT,
        "startup.config",
        extra={
            "mode": cfg.mode,
            "static_symbols_count": len(cfg.static_symbols),
            "active_key": cfg.active_key,
            "filter_stream_channel": cfg.filter_stream_channel,
            "raise_enable": cfg.raise_enable,
            "raise_tier": cfg.raise_tier,
            "pg_dsn_present": bool(cfg.pg_dsn),
            "price_max": cfg.price_max,
            "float_allow_null": cfg.float_allow_null,
            "float_min": cfg.float_min,
            "float_max": cfg.float_max,
            "refresh_sec": cfg.refresh_sec,
        },
    )

    r = aioredis.from_url(cfg.redis_url, decode_responses=True)

    state: Dict[str, Any] = {"active_count": 0, "last_refresh_ts": 0.0}
    hb = asyncio.create_task(_heartbeat_loop(cfg, state))

    try:
        # Always start from a clean authoritative set (FTS owns this key)
        await r.delete(cfg.active_key)

        if cfg.mode == "static":
            universe = set(cfg.static_symbols)
            # seed redis + pubsub
            if universe:
                await r.sadd(cfg.active_key, *sorted(universe))
                for sym in sorted(universe):
                    await publish_async(cfg.filter_stream_channel, {"kind": "add", "symbol": sym, "ts": time.time(), "source": COMPONENT})

            # Raise tiers (WATCH) so DataHub actually produces/keeps bars for them
            if cfg.raise_enable and universe:
                for sym in sorted(universe):
                    await _raise_tier(sym, cfg.raise_tier, cfg.raise_channel)
                    if cfg.tier_publish_sleep_s:
                        await asyncio.sleep(cfg.tier_publish_sleep_s)

            state["active_count"] = len(universe)
            state["last_refresh_ts"] = time.time()

            log.info(COMPONENT, "static.seeded", extra={"count": len(universe)})

            # Static mode: just idle; membership is fixed until restart
            while True:
                await asyncio.sleep(3600)

        # DBSCAN mode
        old: Set[str] = set()

        while True:
            t0 = time.time()

            if not cfg.pg_dsn:
                log.error(COMPONENT, "dbscan.pg_dsn_missing", extra={"env": ["REFLEX_PG_DSN", "PG_DSN", "DATABASE_URL"]})
                new = set()
            else:
                new = await asyncio.to_thread(
                    _load_dbscan_universe,
                    cfg.pg_dsn,
                    cfg.price_max,
                    cfg.float_allow_null,
                    cfg.float_min,
                    cfg.float_max,
                )

            adds, rems = await _emit_membership_deltas(cfg, r, new, old)

            # Raise tiers for newly added symbols
            if cfg.raise_enable and adds:
                for sym in sorted(new - old):
                    await _raise_tier(sym, cfg.raise_tier, cfg.raise_channel)
                    if cfg.tier_publish_sleep_s:
                        await asyncio.sleep(cfg.tier_publish_sleep_s)

            old = new
            state["active_count"] = len(old)
            state["last_refresh_ts"] = time.time()

            log.info(
                COMPONENT,
                "dbscan.refresh",
                extra={
                    "count": len(old),
                    "adds": adds,
                    "rems": rems,
                    "took_ms": int((time.time() - t0) * 1000),
                },
            )

            await asyncio.sleep(max(1.0, cfg.refresh_sec))

    except KeyboardInterrupt:
        log.info(COMPONENT, "shutdown.keyboard_interrupt")
    finally:
        hb.cancel()
        try:
            await r.aclose()
        except Exception:
            pass


def main() -> None:
    if os.name == "nt":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
        except Exception:
            pass
    asyncio.run(run())


if __name__ == "__main__":
    main()
