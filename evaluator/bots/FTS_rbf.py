# evaluator/bots/FTS_rbf.py
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis

from datetime import datetime, timezone

def ts_iso(ts: float | None = None) -> str:
    t = ts if ts is not None else time.time()
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# -----------------------------------------------------------------------------
# Make sure project ROOT is on sys.path BEFORE importing project modules
# -----------------------------------------------------------------------------
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# -----------------------------------------------------------------------------
# Try to import common.bus; provide fallbacks if missing or incomplete
# -----------------------------------------------------------------------------
try:
    from common.bus import CHANNELS as _CHANNELS  # type: ignore
    from common.bus import unpack as _unpack      # type: ignore
    from common.bus import publish_async as _publish_async  # type: ignore
    from common.bus import subscribe as _subscribe          # type: ignore
except Exception:
    _CHANNELS = {"raise": "eval.raisetier"}

    def _unpack(data: Any) -> Any:
        # Common shapes: str JSON, bytes JSON, already-a-dict
        if isinstance(data, (dict, list)):
            return data
        if isinstance(data, (bytes, bytearray)):
            try:
                data = data.decode("utf-8", errors="ignore")
            except Exception:
                return None
        if isinstance(data, str):
            s = data.strip()
            if not s:
                return None
            try:
                return json.loads(s)
            except Exception:
                return None
        return None

    async def _publish_async(channel: str, obj: Any) -> None:
        redis_url = os.getenv("GARNET_URL") or os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0"
        r = aioredis.from_url(redis_url, decode_responses=True)
        try:
            payload = obj if isinstance(obj, str) else json.dumps(obj, separators=(",", ":"), default=str)
            await r.publish(channel, payload)
        finally:
            try:
                await r.aclose()
            except Exception:
                pass

    async def _subscribe(channel: str):
        redis_url = os.getenv("GARNET_URL") or os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0"
        r = aioredis.from_url(redis_url, decode_responses=True)
        ps = r.pubsub()
        await ps.subscribe(channel)
        # attach redis so caller can close it if needed
        ps._reflex_redis = r  # type: ignore[attr-defined]
        return ps


CHANNELS = _CHANNELS
unpack = _unpack
publish_async = _publish_async
subscribe = _subscribe


# -----------------------------------------------------------------------------
# Minimal structured logger (so this file is self-contained and won’t crash)
# -----------------------------------------------------------------------------
class _Log:
    @staticmethod
    def info(component: str, msg: str, extra: Optional[dict] = None) -> None:
        now = time.time()
        evt = {"ts": now, "ts_iso": ts_iso(now), "level": "INFO", "component": component, "msg": msg}
        if extra:
            evt["extra"] = extra
        print(json.dumps(evt, default=str))

    @staticmethod
    def error(component: str, msg: str, extra: Optional[dict] = None) -> None:
        now = time.time()
        evt = {"ts": now, "ts_iso": ts_iso(now), "level": "ERROR", "component": component, "msg": msg}
        if extra:
            evt["extra"] = extra
        print(json.dumps(evt, default=str))


log = _Log()

COMPONENT = "eval.fts_rbf"
FTS_RBF_VERSION = "refactor_fix_1"

ET = ZoneInfo("America/New_York")

# Extended hours window used by your RVOL logic
EXT_START_MIN_ET = 4 * 60    # 04:00 ET
EXT_END_MIN_ET = 20 * 60     # 20:00 ET
EXT_LEN_MINUTES = EXT_END_MIN_ET - EXT_START_MIN_ET  # 960 mins


# -----------------------------------------------------------------------------
# Helpers (bar parsing + time decoding)
# -----------------------------------------------------------------------------
def _t_to_epoch_seconds(t_val: Any) -> Optional[float]:
    if t_val is None:
        return None
    try:
        t = int(t_val)
    except Exception:
        return None

    # Typical epochs:
    #   seconds: ~1e9 (2026)
    #   millis:  ~1e12
    #   micros:  ~1e15
    #   nanos:   ~1e18
    if t >= 10**17:      # ns
        return t / 1_000_000_000
    if t >= 10**14:      # us
        return t / 1_000_000
    if t >= 10**11:      # ms   (THIS is the important change)
        return t / 1_000
    if t >= 10**9:       # s
        return float(t)

    # Too small to be an epoch timestamp; treat as invalid
    return None



def _minute_of_day_rth_from_epoch_seconds(t_sec: float) -> Optional[int]:
    """
    Returns minute index inside EXT window [04:00, 20:00).
    """
    dt_et = datetime.fromtimestamp(t_sec, tz=timezone.utc).astimezone(ET)
    m = dt_et.hour * 60 + dt_et.minute
    if m < EXT_START_MIN_ET or m >= EXT_END_MIN_ET:
        return None
    return m - EXT_START_MIN_ET


def _trading_day_et_from_epoch_seconds(t_sec: float) -> str:
    dt_et = datetime.fromtimestamp(t_sec, tz=timezone.utc).astimezone(ET)
    return dt_et.strftime("%Y-%m-%d")


def _extract_bar(obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    Tolerant extractor for multiple possible shapes.
    Normalizes to keys: sym, t, c, v
    """

    # --- unwrap common envelopes ------------------------------------------------
    for k in ("data", "payload", "message", "msg"):
        if k in obj and isinstance(obj[k], dict):
            obj = obj[k]

    if "bar" in obj and isinstance(obj["bar"], dict):
        obj = obj["bar"]

    if "bars" in obj:
        if isinstance(obj["bars"], dict):
            obj = obj["bars"]
        elif isinstance(obj["bars"], list) and obj["bars"] and isinstance(obj["bars"][0], dict):
            obj = obj["bars"][0]

    sym = (
        obj.get("sym")
        or obj.get("symbol")
        or obj.get("S")
        or obj.get("ticker")
        or obj.get("TICKER")
        or ""
    )
    sym = str(sym).strip().upper()

    t = (
        obj.get("t")
        or obj.get("ts")
        or obj.get("T")
        or obj.get("timestamp")
        or obj.get("start_ts")
        or obj.get("end_ts")
        or obj.get("start")
        or obj.get("end")
    )

    c = (
        obj.get("c")
        or obj.get("close")
        or obj.get("C")
        or obj.get("px_close")
        or obj.get("price")
        or obj.get("last")
    )

    v = (
        obj.get("v")
        or obj.get("volume")
        or obj.get("V")
        or obj.get("vol")
        or 0
    )

    try:
        v = int(v or 0)
    except Exception:
        v = 0

    try:
        c = float(c) if c is not None else None
    except Exception:
        c = None

    return {"sym": sym, "t": t, "c": c, "v": v}


def _resolve_path_under_root(path: str) -> str:
    """
    If `path` is relative, treat it as relative to ROOT.
    """
    p = (path or "").strip()
    if not p:
        return ""
    if os.path.isabs(p):
        return p
    return os.path.abspath(os.path.join(ROOT, p))


def _append_jsonl(path: str, obj: Dict[str, Any]) -> None:
    """
    Safe, minimal append-only JSONL logger.
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps(obj, separators=(",", ":"), default=str)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        # We do NOT throw from logging. Ever.
        pass


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
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

    # Disk event log (new)
    event_log_enable: bool
    event_log_path: str

    # RVOL debug (new)
    debug_rvol: bool
    debug_rvol_active_only: bool
    debug_rvol_once_per_min: bool

    # Stage 1
    price_min: float
    price_max: float
    float_min: int
    float_max: int
    max_watch: int
    exch_allow: Set[str]

    # Stage 2
    pct_up_min: float
    pct_up_exit: float
    min_active_hold_s: float
    rvol_5m_min: float
    rvol_cum_min: float

    tier_publish_sleep_s: float
    heartbeat_sec: float


def _truthy(val, default=False):
    if val is None:
        return default
    v = str(val).strip().lower()
    if v in ("1", "true", "t", "yes", "y", "on"):
        return True
    if v in ("0", "false", "f", "no", "n", "off", ""):
        return False
    return default


def load_config() -> Config:
    redis_url = os.getenv("GARNET_URL") or os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0"
    pg_dsn = os.getenv("REFLEX_PG_DSN") or os.getenv("PG_DSN") or os.getenv("DATABASE_URL") or ""

    bars_channel = (
        os.getenv("FTS_RBF_BARS_CHANNEL")
        or os.getenv("REFLEX_DATAHUB_BARS1M_PUB_LIVE")
        or "hub.bars1m.pub.live"
    )

    raise_channel = (
        os.getenv("TIERS_CMD_Q")
        or os.getenv("REFLEX_RAISE_CHANNEL")
        or os.getenv("FTS_RBF_RAISE_CHANNEL")
        or os.getenv("EVAL_RAISE_CHANNEL")
        or CHANNELS.get("raise", "eval.raisetier")
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

    # --- NEW: disk event log ----------------------------------------------------
    event_log_enable = _truthy(os.getenv("FTS_RBF_EVENT_LOG_ENABLE", "0"), default=False)
    event_log_path = _resolve_path_under_root(os.getenv("FTS_RBF_EVENT_LOG_PATH", "logs/fts_rbf_events.jsonl"))

    # --- NEW: RVOL debug --------------------------------------------------------
    debug_rvol = _truthy(os.getenv("FTS_RBF_DEBUG_RVOL", "0"), default=False)
    debug_rvol_active_only = _truthy(os.getenv("FTS_RBF_DEBUG_RVOL_ACTIVE_ONLY", "1"), default=True)
    debug_rvol_once_per_min = _truthy(os.getenv("FTS_RBF_DEBUG_RVOL_ONCE_PER_MIN", "1"), default=True)

    price_min = float(os.getenv("FTS_RBF_PRICE_MIN", "1"))
    price_max = float(os.getenv("FTS_RBF_PRICE_MAX", "20"))
    float_min = int(os.getenv("FTS_RBF_FLOAT_MIN", "1000000"))
    float_max = int(os.getenv("FTS_RBF_FLOAT_MAX", "20000000"))
    max_watch = int(os.getenv("FTS_RBF_MAX_WATCH", "1000"))

    exch_allow_raw = os.getenv("FTS_RBF_EXCH_ALLOW", "NASDAQ,NYSE,AMEX")
    exch_allow = {s.strip().upper() for s in exch_allow_raw.split(",") if s.strip()}

    pct_up_min = float(os.getenv("FTS_RBF_STAGE2_PCT_UP_MIN", "8"))
    pct_up_exit = float(os.getenv("FTS_RBF_STAGE2_PCT_UP_EXIT", str(max(0.0, pct_up_min - 4.0))))
    min_active_hold_s = float(os.getenv("FTS_RBF_MIN_ACTIVE_HOLD_S", "180"))
    rvol_5m_min = float(os.getenv("FTS_RBF_STAGE2_RVOL_5M_MIN", "4"))
    rvol_cum_min = float(os.getenv("FTS_RBF_STAGE2_RVOL_CUM_MIN", "0"))

    tier_publish_sleep_s = float(os.getenv("FTS_RBF_TIER_PUBLISH_SLEEP_S", "0.002"))
    heartbeat_sec = float(os.getenv("FTS_RBF_HEARTBEAT_SEC", "30"))

    if prefetch_pct_up_hysteresis > prefetch_pct_up_min:
        prefetch_pct_up_hysteresis = max(0.0, prefetch_pct_up_min - 1.0)
    if max_watch < 1:
        max_watch = 1

    log.info(
        COMPONENT,
        "config.loaded",
        extra={
            "history_limit": history_limit,
            "history_lookback_days": history_lookback_days,
            "history_min_days": history_min_days,
            "history_concurrency": history_concurrency,
            "http_timeout_s": http_timeout_s,
            "prefetch_enable": prefetch_enable,
            "prefetch_pct_up_min": prefetch_pct_up_min,
            "prefetch_pct_up_hysteresis": prefetch_pct_up_hysteresis,
            "prefetch_always_for_active": prefetch_always_for_active,
            "prefetch_min_bars": prefetch_min_bars,
            "prefetch_min_age_s": prefetch_min_age_s,
            "prefetch_max_inflight": prefetch_max_inflight,
            "universe_key": universe_key,
            "active_key": active_key,
            "filter_stream_channel": filter_stream_channel,
            "durable_log_key": durable_log_key,
            "durable_log_maxlen": durable_log_maxlen,
            "durable_log_reset": durable_log_reset,
            "publish_pubsub": publish_pubsub,
            "price_min": price_min,
            "price_max": price_max,
            "float_min": float_min,
            "float_max": float_max,
            "max_watch": max_watch,
            "exch_allow": sorted(exch_allow),
            "pct_up_min": pct_up_min,
            "pct_up_exit": pct_up_exit,
            "min_active_hold_s": min_active_hold_s,
            "rvol_5m_min": rvol_5m_min,
            "rvol_cum_min": rvol_cum_min,
            "tier_publish_sleep_s": tier_publish_sleep_s,
            "heartbeat_sec": heartbeat_sec,
        },
    )

    log.info(
        COMPONENT,
        "startup.config",
        extra={
            "redis_url": redis_url,
            "pg_dsn_set": bool(pg_dsn),
            "bars_channel": bars_channel,
            "raise_channel": raise_channel,
            "datahub_url": datahub_url,
            "event_log_enable": event_log_enable,
            "event_log_path": event_log_path if event_log_enable else None,
            "debug_rvol": debug_rvol,
        },
    )



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
        event_log_enable=event_log_enable,
        event_log_path=event_log_path,
        debug_rvol=debug_rvol,
        debug_rvol_active_only=debug_rvol_active_only,
        debug_rvol_once_per_min=debug_rvol_once_per_min,
        price_min=price_min,
        price_max=price_max,
        float_min=float_min,
        float_max=float_max,
        max_watch=max_watch,
        exch_allow=exch_allow,
        pct_up_min=pct_up_min,
        pct_up_exit=pct_up_exit,
        min_active_hold_s=min_active_hold_s,
        rvol_5m_min=rvol_5m_min,
        rvol_cum_min=rvol_cum_min,
        tier_publish_sleep_s=tier_publish_sleep_s,
        heartbeat_sec=heartbeat_sec,
    )


# -----------------------------------------------------------------------------
# Universe bootstrap (same as your file, just not allowed to crash)
# -----------------------------------------------------------------------------
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
    log.info(COMPONENT, "bootstrap.begin", extra={"version": FTS_RBF_VERSION, "ext_window_et": "04:00-20:00"})
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


# -----------------------------------------------------------------------------
# RVOL baseline + history fetch
# -----------------------------------------------------------------------------
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
        m = dt_et.hour * 60 + dt_et.minute
        if m < EXT_START_MIN_ET or m >= EXT_END_MIN_ET:
            continue
        idx = m - EXT_START_MIN_ET
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
        return None

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
    if extra:
        evt.update(extra)

    # 1) Pubsub (unchanged)
    if cfg.publish_pubsub:
        try:
            await publish_async(cfg.filter_stream_channel, evt)
        except Exception as exc:
            log.error(COMPONENT, "stream.pubsub_publish_failed", extra={"error": repr(exc)})

    # 2) Redis durable list (unchanged)
    try:
        entry = json.dumps(evt, separators=(",", ":"), default=str)
        await r.rpush(cfg.durable_log_key, entry)
        if cfg.durable_log_maxlen > 0:
            await r.ltrim(cfg.durable_log_key, -cfg.durable_log_maxlen, -1)
    except Exception as exc:
        log.error(COMPONENT, "durable_log.rpush_failed", extra={"error": repr(exc)})

    # 3) Disk JSONL (NEW, very small, never throws)
    if cfg.event_log_enable and cfg.event_log_path:
        _append_jsonl(cfg.event_log_path, evt)


def _pct_up(close_now: float, prev_close: float) -> float:
    if prev_close <= 0:
        return 0.0
    return ((close_now - prev_close) / prev_close) * 100.0


def _stage2_passes(cfg: Config, pct_up: float, rvol_5m: float, rvol_cum: float) -> bool:
    if pct_up < cfg.pct_up_min:
        return False
    if rvol_5m < cfg.rvol_5m_min:
        return False
    if cfg.rvol_cum_min > 0 and rvol_cum < cfg.rvol_cum_min:
        return False
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

    baseline_cache: Dict[str, RvolBaseline] = {}
    seen_bars: Dict[str, int] = {}
    first_seen_sec: Dict[str, float] = {}
    prefetch_sem = asyncio.Semaphore(cfg.prefetch_max_inflight)

    baseline_inflight: Dict[str, asyncio.Task[Optional[RvolBaseline]]] = {}
    baseline_sem = asyncio.Semaphore(max(1, cfg.history_concurrency))

    active: Set[str] = set()
    active_since: Dict[str, float] = {}
    prefetch_zone: Set[str] = set()
    live_state: Dict[str, LiveRvolState] = {}

    # NEW: per-symbol RVOL debug throttle
    last_dbg_minute: Dict[str, int] = {}

    async def get_baseline(symbol: str) -> Optional[RvolBaseline]:
        if symbol in baseline_cache:
            return baseline_cache[symbol]
        if symbol in baseline_inflight:
            return await baseline_inflight[symbol]

        async def _load() -> Optional[RvolBaseline]:
            async with baseline_sem:
                try:
                    bars = await hc.fetch_bars(symbol, cfg.history_limit)
                    bl = _build_rvol_baseline_from_history(bars, cfg.history_lookback_days, cfg.history_min_days)
                    if bl is None:
                        return None
                    baseline_cache[symbol] = bl
                    log.info(COMPONENT, "rvol.baseline.loaded", extra={"symbol": symbol, "days_used": bl.days_used})
                    return bl
                except Exception as exc:
                    log.error(COMPONENT, "rvol.baseline.error", extra={"symbol": symbol, "error": repr(exc)})
                    return None
                finally:
                    baseline_inflight.pop(symbol, None)

        t = asyncio.create_task(_load())
        baseline_inflight[symbol] = t
        return await t

    def should_prefetch(symbol: str, pct_up: float) -> bool:
        if not cfg.prefetch_enable:
            return True
        if cfg.prefetch_always_for_active and symbol in active:
            return True
        if symbol in baseline_cache:
            return True
        if symbol in prefetch_zone:
            return pct_up >= cfg.prefetch_pct_up_hysteresis
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
            log.info(
                COMPONENT,
                "heartbeat",
                extra={
                    "msgs_seen": msgs_seen,
                    "bars_seen": bars_seen,
                    "active": len(active),
                    "baseline_cached": len(baseline_cache),
                    "last_bar_age_s": age,
                },
            )

    hb_task = asyncio.create_task(heartbeat())

    try:
        async for msg in ps.listen():  # type: ignore[attr-defined]
            if msg.get("type") != "message":
                continue
            msgs_seen += 1

            obj = unpack(msg.get("data"))
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
                continue

            if cfg.prefetch_enable:
                if pct >= cfg.prefetch_pct_up_min:
                    prefetch_zone.add(sym)
                elif sym in prefetch_zone and pct < cfg.prefetch_pct_up_hysteresis:
                    prefetch_zone.discard(sym)

            bypass_gate = (sym in active) or (sym in baseline_cache) or (sym in baseline_inflight)
            if not bypass_gate:
                if seen_bars.get(sym, 0) < cfg.prefetch_min_bars:
                    continue
                if (time.time() - first_seen_sec.get(sym, time.time())) < cfg.prefetch_min_age_s:
                    continue

            if sym in baseline_cache:
                bl = baseline_cache[sym]
            else:
                async with prefetch_sem:
                    bl = await get_baseline(sym)
            if bl is None:
                continue

            day = _trading_day_et_from_epoch_seconds(t_sec)
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
            v5_sum = st.v5_sum

            rvol_cum = (st.cum_today / exp_cum) if exp_cum > 0 else 0.0
            rvol_5m = (v5_sum / exp_5m) if exp_5m > 0 else 0.0

            ok = _stage2_passes(cfg, pct, rvol_5m, rvol_cum)

            # NEW: verbose RVOL debug (throttled)
            if cfg.debug_rvol:
                if not cfg.debug_rvol_active_only or (sym in prefetch_zone) or (sym in active):
                    emit_dbg = True
                    if cfg.debug_rvol_once_per_min:
                        last_m = last_dbg_minute.get(sym)
                        if last_m == m:
                            emit_dbg = False
                        else:
                            last_dbg_minute[sym] = m
                    if emit_dbg:
                        log.info(
                            COMPONENT,
                            "rvol.debug",
                            extra={
                                "symbol": sym,
                                "day": day,
                                "minute_idx": m,
                                "bar_v": v,
                                "v5_sum": v5_sum,
                                "cum_today": st.cum_today,
                                "exp_5m": round(float(exp_5m), 3),
                                "exp_cum": round(float(exp_cum), 3),
                                "rvol_5m": round(rvol_5m, 6),
                                "rvol_cum": round(rvol_cum, 6),
                                "pct_up": round(pct, 6),
                                "stage2_ok": bool(ok),
                            },
                        )

            bars_seen += 1
            last_bar_wall = time.time()

            if ok and sym not in active:
                active.add(sym)
                active_since[sym] = now_sec
                await r.sadd(cfg.active_key, sym)
                await _raise_tier(cfg, sym, "WARM")
                await _emit_change(
                    cfg,
                    r,
                    "add",
                    sym,
                    extra={"pct_up": round(pct, 3), "rvol_5m": round(rvol_5m, 3), "rvol_cum": round(rvol_cum, 3)},
                )
                log.info(COMPONENT, "stage2.add", extra={"symbol": sym, "pct_up": pct, "rvol_5m": rvol_5m, "rvol_cum": rvol_cum})

            elif sym in active:
                since = active_since.get(sym, now_sec)
                if (now_sec - since) < cfg.min_active_hold_s:
                    continue
                if pct >= cfg.pct_up_exit:
                    continue
                if ok:
                    continue

                active.remove(sym)
                active_since.pop(sym, None)
                await r.srem(cfg.active_key, sym)
                await _raise_tier(cfg, sym, "WATCH")
                await _emit_change(
                    cfg,
                    r,
                    "remove",
                    sym,
                    extra={"pct_up": round(pct, 3), "rvol_5m": round(rvol_5m, 3), "rvol_cum": round(rvol_cum, 3)},
                )
                log.info(COMPONENT, "stage2.remove", extra={"symbol": sym, "pct_up": pct, "rvol_5m": rvol_5m, "rvol_cum": rvol_cum})

    except KeyboardInterrupt:
        log.info(COMPONENT, "shutdown.keyboard_interrupt")
    finally:
        hb_task.cancel()
        try:
            rr = getattr(ps, "_reflex_redis", None)
            if rr is not None:
                try:
                    await rr.aclose()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            await r.aclose()
        except Exception:
            pass


def main() -> None:
    cfg = load_config()
    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
