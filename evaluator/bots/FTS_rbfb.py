# evaluator/bots/FTS_rbf.py
from __future__ import annotations

import asyncio
import json
import os
import sys
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis

def _normalize_raise_channel(ch: str) -> str:
    """Normalize tier command channel to global name (single-DataHub runtime).

    If someone passes an instance-scoped channel like:
      reflex:liveA:cmd.tiers
    normalize it to:
      reflex:cmd.tiers
    """
    ch = (ch or "").strip()
    if not ch:
        return "reflex:cmd.tiers"
    if re.match(r"^reflex:[^:]+:cmd\.tiers$", ch):
        return "reflex:cmd.tiers"
    return ch


# ---------------------------------------------------------------------------
# repo-root sys.path bootstrap + .env/.env.local loader (KISS)
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



from common import logging as log
from common.bus import CHANNELS, publish_async, subscribe, unpack

COMPONENT = "eval.fts_rbf"
FTS_RBF_VERSION = "premarket_stage15_v2"
ET = ZoneInfo("America/New_York")

# Extended-hours session window (ET): 04:00–20:00
EXT_START_MIN_ET = 4 * 60
EXT_END_MIN_ET = 20 * 60
EXT_LEN_MINUTES = EXT_END_MIN_ET - EXT_START_MIN_ET  # 960



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truthy(s: str | None, default: bool = False) -> bool:
    if s is None:
        return default
    return (s.strip().lower() not in ("0", "false", "no", "off", ""))


def _f(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None


def _i(x: Any) -> Optional[int]:
    if x is None:
        return None
    try:
        return int(x)
    except Exception:
        return None


def _extract_bar(d: Dict[str, Any]) -> Dict[str, Any]:
    # Match TAP_bars.py field fallbacks exactly. :contentReference[oaicite:5]{index=5}
    sym = d.get("sym") or d.get("symbol") or d.get("S")
    o = d.get("o") or d.get("open")
    h = d.get("h") or d.get("high")
    l = d.get("l") or d.get("low")
    c = d.get("c") or d.get("close")
    v = d.get("v") or d.get("volume")
    vw = d.get("vw") or d.get("vwap")
    n = d.get("n") or d.get("trades")
    t = d.get("t") or d.get("ts") or d.get("bar_ts") or d.get("timestamp") or d.get("minute_key")

    return {
        "sym": str(sym).upper() if sym else None,
        "t": t,
        "o": _f(o),
        "h": _f(h),
        "l": _f(l),
        "c": _f(c),
        "v": _i(v) or 0,
        "vw": _f(vw),
        "n": _i(n),
    }


def _t_to_epoch_seconds(t: Any) -> Optional[float]:
    """
    Convert a bar time field to epoch seconds.
    Mirrors TAP_bars _fmt_ts ms-epoch handling. :contentReference[oaicite:6]{index=6}
    Accepts:
      - epoch seconds
      - epoch milliseconds
      - ISO strings (best-effort)
    """
    if t is None:
        return None
    if isinstance(t, str):
        s = t.strip()
        # If it's numeric in string form
        try:
            n = float(s)
            # ms epoch
            if n > 10_000_000_000:
                n = n / 1000.0
            return n
        except Exception:
            pass
        # ISO
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return None

    n = _f(t)
    if n is None:
        return None
    # ms epoch heuristic
    if n > 10_000_000_000:
        n = n / 1000.0
    return n


def _epoch_seconds_to_ns(sec: float) -> int:
    return int(sec * 1_000_000_000)


def _minute_of_day_rth_from_epoch_seconds(sec: float) -> Optional[int]:
    """Return the extended-hours minute index (ET) in [0..959] for 04:00–20:00.

    NOTE: Name kept for backwards compatibility inside this bot; it is no longer RTH-only.
    Returns None if timestamp is outside 04:00–20:00 ET.
    """
    ts_ns = _epoch_seconds_to_ns(sec)
    dt_utc = datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=timezone.utc)
    dt_et = dt_utc.astimezone(ET)
    m = dt_et.hour * 60 + dt_et.minute
    if m < EXT_START_MIN_ET or m >= EXT_END_MIN_ET:
        return None
    return m - EXT_START_MIN_ET


def _trading_day_et_from_epoch_seconds(sec: float) -> str:
    dt_utc = datetime.fromtimestamp(sec, tz=timezone.utc)
    dt_et = dt_utc.astimezone(ET)
    return dt_et.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Config:
    redis_url: str
    pg_dsn: str

    bars_channel: str
    raise_channel: str

    datahub_url: str
    history_limit: int
    history_lookback_days: int
    history_min_days: int
    history_concurrency: int
    http_timeout_s: float

    prefetch_enable: bool
    prefetch_pct_up_min: float
    prefetch_pct_up_hysteresis: float
    prefetch_always_for_active: bool
    prefetch_min_bars: int
    prefetch_min_age_s: float
    prefetch_max_inflight: int


    universe_key: str
    active_key: str
    filter_stream_channel: str

    durable_log_key: str
    durable_log_maxlen: int
    durable_log_reset: bool
    publish_pubsub: bool

    # Stage 1
    price_min: float
    price_max: float
    float_min: int
    float_max: int
    max_watch: int
    exch_allow: Set[str]

    # Stage 2
    pct_up_min: float
    rvol_5m_min: float
    rvol_cum_min: float

    tier_publish_sleep_s: float
    heartbeat_sec: float


def load_config() -> Config:
    redis_url = os.getenv("GARNET_URL") or os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0"
    pg_dsn = os.getenv("REFLEX_PG_DSN") or os.getenv("PG_DSN") or os.getenv("DATABASE_URL") or ""

    bars_channel = (
        os.getenv("FTS_RBF_BARS_CHANNEL")
        or os.getenv("REFLEX_DATAHUB_BARS1M_PUB_LIVE")
        or "hub.bars1m.pub.live"
    )
    raise_channel = _normalize_raise_channel(
        os.getenv("REFLEX_RAISE_CHANNEL")
        or os.getenv("FTS_RBF_RAISE_CHANNEL")
        or os.getenv("EVAL_RAISE_CHANNEL")
        or CHANNELS.get("raise", "reflex:cmd.tiers")
    )
    datahub_url = (os.getenv("DATAHUB_URL") or os.getenv("HUB_API_URL") or "http://127.0.0.1:7000").rstrip("/")
    history_limit = int(os.getenv("FTS_RBF_HISTORY_LIMIT", "12000"))
    history_lookback_days = int(os.getenv("FTS_RBF_HISTORY_LOOKBACK_DAYS", "30"))
    history_min_days = int(os.getenv("FTS_RBF_HISTORY_MIN_DAYS", "10"))
    history_concurrency = int(os.getenv("FTS_RBF_HISTORY_CONCURRENCY", "4"))
    http_timeout_s = float(os.getenv("FTS_RBF_HTTP_TIMEOUT_S", "8"))

    prefetch_enable = _truthy(os.getenv("FTS_RBF_PREFETCH_ENABLE", "1"), default=True)
    prefetch_pct_up_min = float(os.getenv("FTS_RBF_PREFETCH_PCT_UP_MIN", "6"))
    prefetch_pct_up_hysteresis = float(os.getenv("FTS_RBF_PREFETCH_PCT_UP_HYSTERESIS", "4"))
    prefetch_always_for_active = _truthy(os.getenv("FTS_RBF_PREFETCH_ALWAYS_FOR_ACTIVE", "1"), default=True)
    prefetch_min_bars = int(os.getenv("FTS_RBF_PREFETCH_MIN_BARS", "5"))
    prefetch_min_age_s = float(os.getenv("FTS_RBF_PREFETCH_MIN_AGE_S", "120"))
    prefetch_max_inflight = int(os.getenv("FTS_RBF_PREFETCH_MAX_INFLIGHT", "4"))


    universe_key = os.getenv("FTS_RBF_UNIVERSE_KEY", "eval:fts_rbf:universe")
    active_key = os.getenv("FTS_RBF_ACTIVE_KEY", "eval:fts_rbf:active")
    filter_stream_channel = os.getenv("FTS_RBF_FILTER_STREAM_CHANNEL", "eval.rbf_filter_stream")

    durable_log_key = os.getenv("FTS_RBF_DURABLE_LOG_KEY", "eval:fts_rbf:durable")
    durable_log_maxlen = int(os.getenv("FTS_RBF_DURABLE_LOG_MAXLEN", "20000"))
    durable_log_reset = _truthy(os.getenv("FTS_RBF_DURABLE_LOG_RESET", "1"), default=True)
    publish_pubsub = _truthy(os.getenv("FTS_RBF_PUBSUB_ENABLE", "1"), default=True)

    price_min = float(os.getenv("FTS_RBF_PRICE_MIN", "1"))
    price_max = float(os.getenv("FTS_RBF_PRICE_MAX", "20"))
    float_min = int(os.getenv("FTS_RBF_FLOAT_MIN", "1000000"))
    float_max = int(os.getenv("FTS_RBF_FLOAT_MAX", "10000000"))
    max_watch = int(os.getenv("FTS_RBF_MAX_WATCH", "600"))

    exch_allow_raw = os.getenv("FTS_RBF_EXCH_ALLOW", "NASDAQ,NYSE,AMEX")
    exch_allow = {s.strip().upper() for s in exch_allow_raw.split(",") if s.strip()}

    pct_up_min = float(os.getenv("FTS_RBF_STAGE2_PCT_UP_MIN", "10"))
    rvol_5m_min = float(os.getenv("FTS_RBF_STAGE2_RVOL_5M_MIN", "5"))
    rvol_cum_min = float(os.getenv("FTS_RBF_STAGE2_RVOL_CUM_MIN", "0"))

    tier_publish_sleep_s = float(os.getenv("FTS_RBF_TIER_PUBLISH_SLEEP_S", "0.002"))
    heartbeat_sec = float(os.getenv("FTS_RBF_HEARTBEAT_SEC", "30"))

    if prefetch_pct_up_hysteresis > prefetch_pct_up_min:
        prefetch_pct_up_hysteresis = max(0.0, prefetch_pct_up_min - 1.0)
    if max_watch < 1:
        max_watch = 1

    print(f"Config: redis_url={redis_url}, pg_dsn={pg_dsn}, bars_channel={bars_channel}, raise_channel={raise_channel}, datahub_url={datahub_url}, history_limit={history_limit}, history_lookback_days={history_lookback_days}, history_min_days={history_min_days}, history_concurrency={history_concurrency}, http_timeout_s={http_timeout_s}, prefetch_enable={prefetch_enable}, prefetch_pct_up_min={prefetch_pct_up_min}, prefetch_pct_up_hysteresis={prefetch_pct_up_hysteresis}, prefetch_always_for_active={prefetch_always_for_active}, prefetch_min_bars={prefetch_min_bars}, prefetch_min_age_s={prefetch_min_age_s}, prefetch_max_inflight={prefetch_max_inflight}, universe_key={universe_key}, active_key={active_key}, filter_stream_channel={filter_stream_channel}, durable_log_key={durable_log_key}, durable_log_maxlen={durable_log_maxlen}, durable_log_reset={durable_log_reset}, publish_pubsub={publish_pubsub}, price_min={price_min}, price_max={price_max}, float_min={float_min}, float_max={float_max}, max_watch={max_watch}, exch_allow={exch_allow}, pct_up_min={pct_up_min}, rvol_5m_min={rvol_5m_min}, rvol_cum_min={rvol_cum_min}, tier_publish_sleep_s={tier_publish_sleep_s}, heartbeat_sec={heartbeat_sec}")

    return Config(
        redis_url=redis_url,
        pg_dsn=pg_dsn,
        bars_channel=bars_channel,
        raise_channel=raise_channel,
        datahub_url=datahub_url,
        history_limit=history_limit,
        history_lookback_days=history_lookback_days,
        history_min_days=history_min_days,
        history_concurrency=history_concurrency,
        http_timeout_s=http_timeout_s,
        prefetch_enable=prefetch_enable,
        prefetch_pct_up_min=prefetch_pct_up_min,
        prefetch_pct_up_hysteresis=prefetch_pct_up_hysteresis,
        prefetch_always_for_active=prefetch_always_for_active,
        prefetch_min_bars=prefetch_min_bars,
        prefetch_min_age_s=prefetch_min_age_s,
        prefetch_max_inflight=prefetch_max_inflight,
        universe_key=universe_key,
        active_key=active_key,
        filter_stream_channel=filter_stream_channel,
        durable_log_key=durable_log_key,
        durable_log_maxlen=durable_log_maxlen,
        durable_log_reset=durable_log_reset,
        publish_pubsub=publish_pubsub,
        price_min=price_min,
        price_max=price_max,
        float_min=float_min,
        float_max=float_max,
        max_watch=max_watch,
        exch_allow=exch_allow,
        pct_up_min=pct_up_min,
        rvol_5m_min=rvol_5m_min,
        rvol_cum_min=rvol_cum_min,
        tier_publish_sleep_s=tier_publish_sleep_s,
        heartbeat_sec=heartbeat_sec,
    )


# ---------------------------------------------------------------------------
# Stage 1: Universe load from PG (fundamental_data float + exchange normalization)
# ---------------------------------------------------------------------------

def _load_universe_from_pg(
    pg_dsn: str,
    price_min: float,
    price_max: float,
    float_min: int,
    float_max: int,
    max_watch: int,
    exch_allow: Set[str],
) -> Dict[str, float]:
    if not pg_dsn:
        return {}

    import psycopg
    import psycopg.rows

    allow = sorted({x.upper() for x in exch_allow if x}) or ["NASDAQ", "NYSE", "AMEX"]
    exch_placeholders = ",".join(["%s"] * len(allow))

    sql = f"""
        WITH fd_norm AS (
          SELECT
            fd.symbol,
            fd.shares_float,
            CASE
              WHEN fd.exchange IN ('XNAS','NASDAQ') THEN 'NASDAQ'
              WHEN fd.exchange IN ('XNYS','NYSE')   THEN 'NYSE'
              WHEN fd.exchange IN ('XASE','AMEX')   THEN 'AMEX'
              ELSE NULL
            END AS exch_norm
          FROM fundamental_data fd
        )
        SELECT sp.symbol, d1.px_close
        FROM symbol_profile_view sp
        JOIN fd_norm f
          ON f.symbol = sp.symbol
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
          AND f.shares_float IS NOT NULL
          AND f.shares_float BETWEEN %s AND %s
          AND f.exch_norm IN ({exch_placeholders})
        ORDER BY f.shares_float ASC
        LIMIT %s
    """

    params: List[Any] = [price_min, price_max, float_min, float_max]
    params.extend(allow)
    params.append(max_watch)

    out: Dict[str, float] = {}
    with psycopg.connect(pg_dsn, row_factory=psycopg.rows.dict_row) as conn:  # type: ignore[arg-type]
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for row in cur:
                sym = (row.get("symbol") or "").strip().upper()
                px = row.get("px_close")
                if not sym or px is None:
                    continue
                try:
                    out[sym] = float(px)
                except Exception:
                    continue
    return out


async def bootstrap_universe(cfg: Config, r: aioredis.Redis) -> Dict[str, float]:
    log.info(COMPONENT, "bootstrap.pg_query_begin")
    log.info(COMPONENT, "version", extra={"version": FTS_RBF_VERSION, "ext_window_et": "04:00-20:00"})
    try:
        sym2close = await asyncio.to_thread(
            _load_universe_from_pg,
            cfg.pg_dsn,
            cfg.price_min,
            cfg.price_max,
            cfg.float_min,
            cfg.float_max,
            cfg.max_watch,
            cfg.exch_allow,
        )
    except Exception as exc:
        log.error(COMPONENT, "bootstrap.pg_query_failed", extra={"error": repr(exc)})
        return {}

    universe = sorted(sym2close.keys())
    await r.delete(cfg.universe_key)
    if universe:
        await r.sadd(cfg.universe_key, *universe)

    log.info(COMPONENT, "bootstrap.redis_universe_populated", extra={"count": len(universe), "bars_channel": cfg.bars_channel})
    return sym2close


async def _raise_tier(cfg: Config, symbol: str, tier: str) -> None:
    evt = {"symbol": symbol, "tier": tier, "ts": time.time(), "source": COMPONENT}
    await publish_async(cfg.raise_channel, evt)
    if cfg.tier_publish_sleep_s > 0:
        await asyncio.sleep(cfg.tier_publish_sleep_s)


# ---------------------------------------------------------------------------
# RVOL baseline
# ---------------------------------------------------------------------------

@dataclass
class RvolBaseline:
    avg_cum: List[float]
    avg_5m: List[float]
    days_used: int


class HistoryClient:
    def __init__(self, base_url: str, timeout_s: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def _fetch_bars_sync(self, symbol: str, limit: int) -> List[Dict[str, Any]]:
        import urllib.parse
        import urllib.request

        qs = urllib.parse.urlencode({"symbol": symbol, "limit": str(limit)})
        url = f"{self.base_url}/v1/history/bars1m?{qs}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8", errors="ignore"))
        if not isinstance(data, dict) or not data.get("ok"):
            return []
        bars = data.get("bars") or []
        if not isinstance(bars, list):
            return []
        out: List[Dict[str, Any]] = []
        for b in bars:
            if not isinstance(b, dict):
                continue
            try:
                ts_ns = int(b.get("ts"))
                v = int(b.get("volume") or 0)
            except Exception:
                continue
            out.append({"ts": ts_ns, "volume": v})
        out.sort(key=lambda x: x["ts"])
        return out

    async def fetch_bars(self, symbol: str, limit: int) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._fetch_bars_sync, symbol, limit)


def _build_rvol_baseline_from_history(bars: List[Dict[str, Any]], lookback_days: int, min_days: int) -> Optional[RvolBaseline]:
    day_vols: Dict[str, List[int]] = {}

    for b in bars:
        ts_ns = int(b["ts"])
        dt_utc = datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=timezone.utc)
        dt_et = dt_utc.astimezone(ET)
        start_min = EXT_START_MIN_ET
        end_min = EXT_END_MIN_ET
        m = dt_et.hour * 60 + dt_et.minute
        if m < start_min or m >= end_min:
            continue
        idx = m - start_min
        day = dt_et.strftime("%Y-%m-%d")
        if day not in day_vols:
            day_vols[day] = [0] * EXT_LEN_MINUTES
        day_vols[day][idx] += int(b.get("volume", 0) or 0)

    if not day_vols:
        return None

    days_sorted = sorted(day_vols.keys())
    if lookback_days > 0 and len(days_sorted) > lookback_days:
        days_sorted = days_sorted[-lookback_days:]

    days_used = len(days_sorted)
    if days_used < max(1, min_days):
        pass

    sum_cum = [0.0] * EXT_LEN_MINUTES
    sum_5m = [0.0] * EXT_LEN_MINUTES

    for day in days_sorted:
        vol = day_vols[day]
        run = 0
        cum = [0] * EXT_LEN_MINUTES
        for i in range(EXT_LEN_MINUTES):
            run += vol[i]
            cum[i] = run

        roll = 0
        v5 = [0] * EXT_LEN_MINUTES
        for i in range(EXT_LEN_MINUTES):
            roll += vol[i]
            if i >= 5:
                roll -= vol[i - 5]
            v5[i] = roll if i >= 4 else 0

        for i in range(EXT_LEN_MINUTES):
            sum_cum[i] += float(cum[i])
            sum_5m[i] += float(v5[i])

    denom = float(max(1, days_used))
    avg_cum = [max(1.0, sum_cum[i] / denom) for i in range(EXT_LEN_MINUTES)]
    avg_5m = [max(1.0, sum_5m[i] / denom) for i in range(EXT_LEN_MINUTES)]
    return RvolBaseline(avg_cum=avg_cum, avg_5m=avg_5m, days_used=days_used)


@dataclass
class LiveRvolState:
    day: str
    cum_today: int
    last5: List[int]

    @property
    def v5_sum(self) -> int:
        return int(sum(self.last5))


async def _emit_change(cfg: Config, r: aioredis.Redis, kind: str, symbol: str, extra: Optional[Dict[str, Any]] = None) -> None:
    evt: Dict[str, Any] = {"kind": kind, "symbol": symbol, "ts": time.time(), "source": COMPONENT}

    print(f"************_EMIT_change **********: evt={evt},filter_stream_channel={cfg.filter_stream_channel}")
    if extra:
        evt.update(extra)


    if cfg.publish_pubsub:
        try:
            await publish_async(cfg.filter_stream_channel, evt)
        except Exception as exc:
            log.error(COMPONENT, "stream.pubsub_publish_failed", extra={"error": repr(exc)})

    try:
        entry = json.dumps(evt, separators=(",", ":"), default=str)
        await r.rpush(cfg.durable_log_key, entry)
        if cfg.durable_log_maxlen > 0:
            await r.ltrim(cfg.durable_log_key, -cfg.durable_log_maxlen, -1)
    except Exception as exc:
        log.error(COMPONENT, "durable_log.rpush_failed", extra={"error": repr(exc)})


def _pct_up(close_now: float, prev_close: float) -> float:
    if prev_close <= 0:
        return 0.0
    return ((close_now - prev_close) / prev_close) * 100.0


def _stage2_passes(cfg: Config, pct_up: float, rvol_5m: float, rvol_cum: float) -> bool:
    print(f"_stage2_passes: pct_up={pct_up}, rvol_5m={rvol_5m}, rvol_cum={rvol_cum}")
    if pct_up < cfg.pct_up_min:
        print(f"_stage2_passes: failed pct_up_min {cfg.pct_up_min}")
        return False
    if rvol_5m < cfg.rvol_5m_min:
        print(f"_stage2_passes: failed rvol_5m_min {cfg.rvol_5m_min}")
        return False
    if cfg.rvol_cum_min > 0 and rvol_cum < cfg.rvol_cum_min:
        print(f"_stage2_passes: failed rvol_cum_min {cfg.rvol_cum_min}")
        return False
    print(f"_stage2_passes: passed")    
    return True


async def run(cfg: Config) -> None:
    r = aioredis.from_url(cfg.redis_url, decode_responses=True)

    await r.delete(cfg.active_key)
    if cfg.durable_log_reset:
        await r.delete(cfg.durable_log_key)

    sym2close = await bootstrap_universe(cfg, r)
    universe = sorted(sym2close.keys())
    if not universe:
        log.error(COMPONENT, "fatal.empty_universe")
        await r.aclose()
        return

    for sym in universe:
        await _raise_tier(cfg, sym, "WATCH")

    hc = HistoryClient(cfg.datahub_url, cfg.http_timeout_s)

    log.info(COMPONENT, "history_client_initialized", extra={"prefetch_pct_up_min": cfg.prefetch_pct_up_min, "prefetch_pct_up_hysteresis": cfg.prefetch_pct_up_hysteresis})
    baseline_cache: Dict[str, RvolBaseline] = {}
    seen_bars: Dict[str, int] = {}
    first_seen_sec: Dict[str, float] = {}
    # baseline_inflight tracks deep-history fetch tasks by symbol (dict below)
    prefetch_sem = asyncio.Semaphore(cfg.prefetch_max_inflight)

    baseline_inflight: Dict[str, asyncio.Task[Optional[RvolBaseline]]] = {}
    baseline_sem = asyncio.Semaphore(max(1, cfg.history_concurrency))

    active: Set[str] = set()
    prefetch_zone: Set[str] = set()
    live_state: Dict[str, LiveRvolState] = {}

    async def get_baseline(symbol: str) -> Optional[RvolBaseline]:
        print(f"get_baseline: symbol={symbol}")
        if symbol in baseline_cache:
            print(f"get_baseline: cache hit for symbol={symbol}")
            return baseline_cache[symbol]
        if symbol in baseline_inflight:
            print(f"get_baseline: inflight hit for symbol={symbol}")
            return await baseline_inflight[symbol]

        print(f"get_baseline: cache miss for symbol={symbol}, loading...")
        async def _load() -> Optional[RvolBaseline]:
            async with baseline_sem:
                try:
                    bars = await hc.fetch_bars(symbol, cfg.history_limit)
                    print(f"BASELINE fetched symbol={symbol} bars={len(bars)}")
                    bl = _build_rvol_baseline_from_history(bars, cfg.history_lookback_days, cfg.history_min_days)
                    print(f"BASELINE built symbol={symbol} ok={bl is not None}")
                    if bl is None:
                        return None
                    baseline_cache[symbol] = bl
                    log.info(COMPONENT, "rvol.baseline.loaded", extra={"symbol": symbol, "days_used": bl.days_used})
                    print(f"get_baseline: loaded baseline for symbol={symbol}, days_used={bl.days_used}")
                    return bl
                except Exception as exc:
                    print(f"BASELINE ERROR symbol={symbol} err={exc!r}")
                    return None
                finally:
                    baseline_inflight.pop(symbol, None)

        t = asyncio.create_task(_load())
        baseline_inflight[symbol] = t
        return await t

    def should_prefetch(symbol: str, pct_up: float) -> bool:

        if not cfg.prefetch_enable:
            print(f"should_prefetch: prefetch not enabled, returning True for symbol={symbol}")
            return True
        if cfg.prefetch_always_for_active and symbol in active:
            print(f"should_prefetch: always prefetch for active symbol={symbol}, returning True")
            return True
        if symbol in baseline_cache:
            print(f"should_prefetch: baseline cached for symbol={symbol}, returning True")
            return True
        if symbol in prefetch_zone:
            print(f"should_prefetch: symbol={symbol} in prefetch_zone, pct_up={pct_up}, returning {pct_up >= cfg.prefetch_pct_up_hysteresis}")
            return pct_up >= cfg.prefetch_pct_up_hysteresis
        print(f"should_prefetch: symbol={symbol} not in prefetch_zone, pct_up={pct_up}, returning {pct_up >= cfg.prefetch_pct_up_min}")
        return pct_up >= cfg.prefetch_pct_up_min

    ps = await subscribe(cfg.bars_channel)
    log.info(COMPONENT, "bars.subscribe_ok", extra={"channel": cfg.bars_channel, "universe": len(universe)})

    msgs_seen = 0
    bars_seen = 0
    last_bar_wall: Optional[float] = None

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(cfg.heartbeat_sec)
            age = round(time.time() - last_bar_wall, 1) if last_bar_wall else None
            log.info(COMPONENT, "heartbeat", extra={"msgs_seen": msgs_seen, "bars_seen": bars_seen, "active": len(active), "baseline_cached": len(baseline_cache), "last_bar_age_s": age})

    hb_task = asyncio.create_task(heartbeat())

    try:
        async for msg in ps.listen():  # type: ignore[attr-defined]
            if msg.get("type") != "message":
                continue
            msgs_seen += 1


            obj = unpack(msg["data"])
            if not isinstance(obj, dict):
                continue

            bar = _extract_bar(obj)

            sym = bar["sym"]
            if not sym or sym not in sym2close:
                continue

            t_sec = _t_to_epoch_seconds(bar["t"])
            if t_sec is None:
                continue

            m = _minute_of_day_rth_from_epoch_seconds(t_sec)
            # debug time decode (ET)
            dt_et = datetime.fromtimestamp(t_sec, tz=timezone.utc).astimezone(ET)
            m_et = dt_et.hour * 60 + dt_et.minute
            if m is None:
                continue
            now_sec = time.time()
            seen_bars[sym] = seen_bars.get(sym, 0) + 1
            if sym not in first_seen_sec:
                first_seen_sec[sym] = now_sec


            c = bar["c"]
            if c is None:
                continue

            prev_close = sym2close.get(sym, 0.0)
            pct = _pct_up(float(c), prev_close)

            eligible = should_prefetch(sym, pct)
            if not eligible:
                # Not a candidate => do NOT load baseline, do NOT compute RVOL.
                continue

            print(f"prefetching symbol={sym}")

            if cfg.prefetch_enable:
                if pct >= cfg.prefetch_pct_up_min:
                    print(f"adding symbol={sym} to prefetch_zone")
                    prefetch_zone.add(sym)
                elif sym in prefetch_zone and pct < cfg.prefetch_pct_up_hysteresis:
                    print(f"removing symbol={sym} from prefetch_zone")
                    prefetch_zone.discard(sym)

            # Stage 1.5 persistence gate (only applies when baseline not already available/inflight/active)
            bypass_gate = (sym in active) or (sym in baseline_cache) or (sym in baseline_inflight)
            if not bypass_gate:
                if seen_bars.get(sym, 0) < cfg.prefetch_min_bars:
                    continue
                if (time.time() - first_seen_sec.get(sym, time.time())) < cfg.prefetch_min_age_s:
                    continue

            # Stage 3: deep baseline load (only reachable if eligible == True)
            if sym in baseline_cache:
                bl = baseline_cache[sym]
            else:
                async with prefetch_sem:
                    bl = await get_baseline(sym)



            if bl is None:
                continue

            day = _trading_day_et_from_epoch_seconds(t_sec)
            print(f"get day symbol={sym}, day={day}")
            st = live_state.get(sym)
            if st is None or st.day != day:
                st = LiveRvolState(day=day, cum_today=0, last5=[])
                live_state[sym] = st

            v = int(bar["v"] or 0)
            st.cum_today += v
            st.last5.append(v)
            if len(st.last5) > 5:
                st.last5 = st.last5[-5:]

            exp_cum = bl.avg_cum[m]
            exp_5m = bl.avg_5m[m]
            rvol_cum = (st.cum_today / exp_cum) if exp_cum > 0 else 0.0
            rvol_5m = (st.v5_sum / exp_5m) if exp_5m > 0 else 0.0

            print(f"get stage 2 symbol={sym}, pct={pct}, rvol_5m={rvol_5m}, rvol_cum={rvol_cum}")
            ok = _stage2_passes(cfg, pct, rvol_5m, rvol_cum)

            bars_seen += 1
            last_bar_wall = time.time()

            if ok and sym not in active:
                active.add(sym)
                await r.sadd(cfg.active_key, sym)
                await _raise_tier(cfg, sym, "WARM")
                await _emit_change(cfg, r, "add", sym, extra={"pct_up": round(pct, 3), "rvol_5m": round(rvol_5m, 3), "rvol_cum": round(rvol_cum, 3)})
                log.info(COMPONENT, "stage2.add", extra={"symbol": sym, "pct_up": pct, "rvol_5m": rvol_5m})

            elif (not ok) and sym in active:
                active.remove(sym)
                await r.srem(cfg.active_key, sym)
                await _raise_tier(cfg, sym, "WATCH")
                await _emit_change(cfg, r, "remove", sym, extra={"pct_up": round(pct, 3), "rvol_5m": round(rvol_5m, 3), "rvol_cum": round(rvol_cum, 3)})
                log.info(COMPONENT, "stage2.remove", extra={"symbol": sym, "pct_up": pct, "rvol_5m": rvol_5m})

    except KeyboardInterrupt:
        log.info(COMPONENT, "shutdown.keyboard_interrupt")
    finally:
        hb_task.cancel()
        try:
            await r.aclose()
        except Exception:
            pass


def main() -> None:
    cfg = load_config()
    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
