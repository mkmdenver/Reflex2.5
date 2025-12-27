from __future__ import annotations

# ------------------------------------------------------------
# Repo-root bootstrap so "import common" works from ANY cwd
# ------------------------------------------------------------
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]  # ...\evaluator\bots -> repo root
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Set

import redis.asyncio as aioredis
import psycopg
import psycopg.rows

from common import logging as log
from common.bus import CHANNELS, subscribe, unpack, publish_async, scoped_key

COMPONENT = "eval.ross_filter_stream"


@dataclass
class Config:
    root: str
    bars_channel: str
    filter_stream_channel: str
    raise_channel: str
    bootstrap_tier: str

    redis_url: str
    universe_key: str
    active_key: str

    price_band: Sequence[float]
    vol_day_min: float
    vol_bar_min: float
    gap_min: float
    gap_max: float

    pg_dsn: str
    heartbeat_sec: float
    debug_always_warm: bool


def load_config() -> Config:
    root = os.getenv("REFLEX_ROOT", str(_REPO_ROOT))

    bars_channel = os.getenv("FTS_RBF_BARS_CHANNEL", "hub.bars1m")

    # PubSub channel (NOT streams). Optional for taps; PTI can run without it.
    filter_stream_channel = os.getenv("FTS_RBF_FILTER_CHANNEL", "eval.fts_rbf.stream")

    raise_channel = CHANNELS["raise"]
    bootstrap_tier = os.getenv("FTS_RBF_BOOTSTRAP_TIER", "WATCH").upper()

    # Explicit and consistent
    redis_url = (
        os.getenv("REDIS_URL")
        or os.getenv("GARNET_URL")
        or os.getenv("REFLEX_REDIS_URL")
        or "redis://127.0.0.1:6379/0"
    )

    # Durable membership keys (Option C) — scoped
    universe_key_raw = os.getenv("FTS_RBF_UNIVERSE_KEY", "eval:fts_rbf:universe")
    active_key_raw = os.getenv("FTS_RBF_ACTIVE_KEY", "eval:fts_rbf:active")
    universe_key = scoped_key(universe_key_raw)
    active_key = scoped_key(active_key_raw)

    # Filters (defaults loose for plumbing verification)
    price_min = float(os.getenv("FTS_RBF_PRICE_MIN", "0"))
    price_max = float(os.getenv("FTS_RBF_PRICE_MAX", "1000"))
    vol_day_min = float(os.getenv("FTS_RBF_VOL_DAY", "0"))
    vol_bar_min = float(os.getenv("FTS_RBF_VOL_BAR", "0"))
    gap_min = float(os.getenv("FTS_RBF_GAP_MIN", "-100"))
    gap_max = float(os.getenv("FTS_RBF_GAP_MAX", "100"))

    pg_dsn = os.getenv("REFLEX_PG_DSN") or os.getenv("REFLEX__PG_DSN") or ""
    if not pg_dsn:
        log.error(COMPONENT, "config.pg_dsn_missing", extra={})

    heartbeat_sec = float(os.getenv("FTS_RBF_HEARTBEAT_SEC", "10"))
    debug_always_warm = os.getenv("FTS_RBF_DEBUG_ALWAYS_WARM", "0") == "1"

    return Config(
        root=root,
        bars_channel=bars_channel,
        filter_stream_channel=filter_stream_channel,
        raise_channel=raise_channel,
        bootstrap_tier=bootstrap_tier,
        redis_url=redis_url,
        universe_key=universe_key,
        active_key=active_key,
        price_band=(price_min, price_max),
        vol_day_min=vol_day_min,
        vol_bar_min=vol_bar_min,
        gap_min=gap_min,
        gap_max=gap_max,
        pg_dsn=pg_dsn,
        heartbeat_sec=heartbeat_sec,
        debug_always_warm=debug_always_warm,
    )


async def _bootstrap_tier_symbol(cfg: Config, symbol: str) -> None:
    payload: Dict[str, Any] = {"symbol": symbol, "tier": cfg.bootstrap_tier, "source": COMPONENT}
    await publish_async(cfg.raise_channel, payload)
    log.info(COMPONENT, "bootstrap_tier.requested", extra={"symbol": symbol, "tier": cfg.bootstrap_tier})


def bootstrap_universe_from_pg(cfg: Config) -> Set[str]:
    sql = (
        "SELECT symbol FROM symbol_profile_view "
        "WHERE shares_float IS NULL OR (shares_float BETWEEN 1000000 AND 20000000)"
    )

    symbols: Set[str] = set()

    if not cfg.pg_dsn:
        log.error(COMPONENT, "bootstrap.pg_dsn_missing", extra={})
        return symbols

    log.info(COMPONENT, "bootstrap.pg_query_begin", extra={})

    with psycopg.connect(cfg.pg_dsn, row_factory=psycopg.rows.dict_row) as conn:  # type: ignore[arg-type]
        with conn.cursor() as cur:
            cur.execute(sql)
            for row in cur:
                sym = row.get("symbol")
                if sym:
                    symbols.add(str(sym).upper())

    log.info(COMPONENT, "bootstrap.pg_candidates_loaded", extra={"count": len(symbols)})
    return symbols


async def _mirror_universe_sets(cfg: Config, universe: Set[str]) -> None:
    client = aioredis.from_url(cfg.redis_url, decode_responses=True)
    try:
        await client.delete(cfg.universe_key)
        await client.delete(cfg.active_key)

        if universe:
            await client.sadd(cfg.universe_key, *universe)

        log.info(
            COMPONENT,
            "bootstrap.redis_sets_populated",
            extra={
                "universe_key": cfg.universe_key,
                "active_key": cfg.active_key,
                "universe_count": len(universe),
                "active_count": 0,
            },
        )
    except Exception as exc:
        log.error(COMPONENT, "bootstrap.redis_error", extra={"error": repr(exc)})
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


async def bootstrap_step_one(cfg: Config) -> Set[str]:
    universe = bootstrap_universe_from_pg(cfg)

    for sym in sorted(universe):
        try:
            await _bootstrap_tier_symbol(cfg, sym)
        except Exception as exc:
            log.error(COMPONENT, "bootstrap_tier.error", extra={"symbol": sym, "error": repr(exc)})

    await _mirror_universe_sets(cfg, universe)
    return universe


def bar_passes_filters(bar: Dict[str, Any], cfg: Config) -> bool:
    price_min, price_max = cfg.price_band

    c = bar.get("c") or bar.get("close")
    v = bar.get("v") or bar.get("volume")
    day_v = bar.get("day_v")
    gap = bar.get("gap")

    try:
        price = float(c) if c is not None else 0.0
    except Exception:
        price = 0.0

    try:
        bar_vol = float(v) if v is not None else 0.0
    except Exception:
        bar_vol = 0.0

    if cfg.debug_always_warm:
        return price > 0 and bar_vol > 0

    if price <= 0 or bar_vol <= 0:
        return False

    if not (price_min <= price <= price_max):
        return False

    if cfg.vol_bar_min > 0 and bar_vol < cfg.vol_bar_min:
        return False

    if day_v is not None and cfg.vol_day_min > 0:
        try:
            day_vol = float(day_v)
        except Exception:
            day_vol = 0.0
        if day_vol < cfg.vol_day_min:
            return False

    if gap is not None:
        try:
            gap_val = float(gap)
        except Exception:
            gap_val = 0.0
        if not (cfg.gap_min <= gap_val <= cfg.gap_max):
            return False

    return True


async def bars_loop(cfg: Config, universe: Set[str]) -> None:
    ps = await subscribe(cfg.bars_channel)
    log.info(COMPONENT, "bars.subscribe_ok", extra={"channel": cfg.bars_channel, "universe_size": len(universe)})

    client = aioredis.from_url(cfg.redis_url, decode_responses=True)

    warm_symbols: Set[str] = set()
    bars_seen = 0
    adds = 0
    removes = 0
    last_hb = time.time()

    try:
        async for msg in ps.listen():  # type: ignore[attr-defined]
            if msg.get("type") != "message":
                continue

            now = time.time()
            if now - last_hb >= cfg.heartbeat_sec:
                log.info(
                    COMPONENT,
                    "bars.heartbeat",
                    extra={
                        "bars_seen": bars_seen,
                        "warm_count": len(warm_symbols),
                        "universe_size": len(universe),
                        "adds_emitted": adds,
                        "removes_emitted": removes,
                        "active_key": cfg.active_key,
                    },
                )
                last_hb = now

            try:
                bar = unpack(msg["data"])
            except Exception as exc:
                log.error(COMPONENT, "bars.unpack_error", extra={"error": repr(exc)})
                continue

            if not isinstance(bar, dict):
                continue

            bars_seen += 1

            sym_raw = bar.get("sym") or bar.get("symbol")
            if not sym_raw:
                continue

            sym = str(sym_raw).upper()
            if sym not in universe:
                continue

            passed = bar_passes_filters(bar, cfg)
            was_warm = sym in warm_symbols

            if passed and not was_warm:
                warm_symbols.add(sym)
                adds += 1

                try:
                    await client.sadd(cfg.active_key, sym)
                except Exception as exc:
                    log.error(COMPONENT, "active_set.sadd_error", extra={"symbol": sym, "error": repr(exc), "key": cfg.active_key})

                evt = {
                    "kind": "add",
                    "symbol": sym,
                    "ts": bar.get("t_recv_ns"),
                    "source": COMPONENT,
                    "tier_hint": "WARM",
                    "active_set_key": cfg.active_key,
                }
                await publish_async(cfg.filter_stream_channel, evt)
                log.info(COMPONENT, "filter.add", extra={"symbol": sym, "warm_count": len(warm_symbols), "adds_emitted": adds})

            elif (not passed) and was_warm:
                warm_symbols.discard(sym)
                removes += 1

                try:
                    await client.srem(cfg.active_key, sym)
                except Exception as exc:
                    log.error(COMPONENT, "active_set.srem_error", extra={"symbol": sym, "error": repr(exc), "key": cfg.active_key})

                evt = {
                    "kind": "remove",
                    "symbol": sym,
                    "ts": bar.get("t_recv_ns"),
                    "source": COMPONENT,
                    "tier_hint": "WARM",
                    "active_set_key": cfg.active_key,
                }
                await publish_async(cfg.filter_stream_channel, evt)
                log.info(COMPONENT, "filter.remove", extra={"symbol": sym, "warm_count": len(warm_symbols), "removes_emitted": removes})

    except asyncio.CancelledError:
        log.info(COMPONENT, "shutdown.cancelled", extra={})
    except Exception as exc:
        log.error(COMPONENT, "shutdown.error", extra={"error": repr(exc)})
    finally:
        try:
            await client.aclose()
        except Exception:
            pass

        log.info(
            COMPONENT,
            "shutdown.complete",
            extra={"bars_seen": bars_seen, "warm_final": len(warm_symbols), "adds_emitted": adds, "removes_emitted": removes},
        )


async def run_bot() -> None:
    cfg = load_config()
    log.info(
        COMPONENT,
        "startup",
        extra={
            "bars_channel": cfg.bars_channel,
            "filter_stream_channel": cfg.filter_stream_channel,
            "raise_channel": cfg.raise_channel,
            "bootstrap_tier": cfg.bootstrap_tier,
            "universe_key": cfg.universe_key,
            "active_key": cfg.active_key,
            "redis_url": cfg.redis_url,
        },
    )

    universe = await bootstrap_step_one(cfg)
    log.info(COMPONENT, "bootstrap.done", extra={"universe_size": len(universe)})

    await bars_loop(cfg, universe)


async def main() -> None:
    await run_bot()


if __name__ == "__main__":
    if os.name == "nt":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
        except Exception:
            pass
    asyncio.run(main())
