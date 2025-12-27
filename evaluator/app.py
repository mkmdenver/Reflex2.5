# evaluator/app.py
from __future__ import annotations

import os, time, logging, asyncio, json
from enum import Enum
from typing import Dict, List, Optional
from typing_extensions import Annotated, Literal

from fastapi import FastAPI, Response, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ConfigDict, field_validator
import redis.asyncio as redis

LOG = logging.getLogger("evaluator.app")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
)

app = FastAPI(title="Reflex Evaluator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Redis / capacity config
# ---------------------------------------------------------------------------

R: Optional[redis.Redis] = None

def _redis_url() -> str:
    return os.getenv("REFLEX_REDIS_URL", "redis://127.0.0.1:6379/0")

CAPACITY_KEY_INFLIGHT = os.getenv(
    "REFLEX__KEY_INFLIGHT", "reflex:capacity:orders_inflight"
)
CAPACITY_KEY_MAX = os.getenv("REFLEX__KEY_MAX", "reflex:capacity:orders_max")


async def _capacity_allow() -> bool:
    """
    Simple gate: increment inflight, compare to max, roll back if > max.
    """
    global R
    if R is None:
        R = redis.from_url(_redis_url(), decode_responses=True)

    max_allowed = int(await R.get(CAPACITY_KEY_MAX) or 8)
    inflight = await R.incr(CAPACITY_KEY_INFLIGHT)

    if inflight > max_allowed:
        await R.decr(CAPACITY_KEY_INFLIGHT)
        return False
    return True


async def _capacity_release() -> None:
    global R
    if R is None:
        return
    await R.decr(CAPACITY_KEY_INFLIGHT)


# ---------------------------------------------------------------------------
# Shared types – these are the canonical intent contract for Evaluator
# ---------------------------------------------------------------------------

class OrderSide(str, Enum):
    buy = "buy"
    sell = "sell"


class OrderType(str, Enum):
    market = "market"
    limit = "limit"
    stop = "stop"
    stop_limit = "stop_limit"
    trailing_stop = "trailing_stop"


class TimeInForce(str, Enum):
    day = "day"
    gtc = "gtc"
    opg = "opg"
    ioc = "ioc"
    fok = "fok"


# This is intentionally the same primitive as Trader expects in /v1/orders
class Symbol(BaseModel):
    value: str

    @field_validator("value")
    @classmethod
    def _upper(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("symbol cannot be empty")
        return v

    def __str__(self) -> str:
        return self.value


class OrderIntent(BaseModel):
    """
    Core order-intent payload that eventually ends up at Trader.

    This matches the envelope Trader/app.py documents for /v1/orders,
    minus some adapter-specific fields that we treat as opaque extras.
    """

    symbol: Symbol
    side: OrderSide
    qty: float = Field(gt=0)
    type: OrderType = OrderType.market
    time_in_force: TimeInForce = TimeInForce.day

    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    trail: Optional[float] = None
    extended_hours: bool = False

    note: Optional[str] = None

    # routing / meta
    account_id: Optional[str] = None
    client_tag: Optional[str] = None  # e.g. "auto" to route into auto queue

    model_config = ConfigDict(extra="allow")


class OrderIntentRequest(BaseModel):
    """
    Thin wrapper for HTTP – Evaluator → Trader auto-intent bridge.

    Kept separate from OrderIntent so we can evolve HTTP details if needed,
    while keeping the bus payload stable.
    """

    intent: OrderIntent
    # optional trace-id override; normally we assign server-side
    trace_id: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ts_utc_ns() -> int:
    return int(time.time() * 1_000_000_000)


def _t_mono_ns() -> int:
    return time.perf_counter_ns()


def _new_trace_id(ts_ns: int, mono_ns: int) -> str:
    # Sortable-ish trace ID
    return f"{ts_ns}-{mono_ns}"


async def _bus_publish(topic: str, payload: Dict) -> None:
    """
    Serialize a simple envelope to the internal bus.

    For now we use Redis pub/sub topic "reflex:events" with a topic field.
    The Trader/worker side is free to adapt this as the contract evolves.
    """
    global R
    if R is None:
        R = redis.from_url(_redis_url(), decode_responses=True)

    ts_ns = _ts_utc_ns()
    mono_ns = _t_mono_ns()
    trace = _new_trace_id(ts_ns, mono_ns)

    envelope = {
        "trace_id": trace,
        "ts_utc_ns": ts_ns,
        "t_mono_ns": mono_ns,
        "topic": topic,
        "payload": payload,
    }

    msg = json.dumps(envelope, separators=(",", ":"))
    await R.publish("reflex:events", msg)


# ---------------------------------------------------------------------------
# Simple service endpoints
# ---------------------------------------------------------------------------

@app.get("/internal/health")
async def internal_health():
    return {"ok": True, "service": "Evaluator"}


@app.get("/v1/health")
async def public_health():
    return {"ok": True}


@app.post("/v1/orders/intent")
async def post_order_intent(req: OrderIntentRequest, response: Response):
    """
    Accept an order-intent from an Evaluator model (or a test harness)
    and enqueue it for Trader.

    This is *not* the BrokerView UI path – that goes directly to Trader.
    """
    if not await _capacity_allow():
        response.status_code = status.HTTP_429_TOO_MANY_REQUESTS
        return {
            "ok": False,
            "error": "capacity_gate_closed",
            "message": "too many intents in flight",
        }

    try:
        payload = req.intent.model_dump()
        # Make sure symbol is a plain string before shipping
        payload["symbol"] = str(req.intent.symbol)

        await _bus_publish("orders.intent", payload)
        return {"ok": True, "trace_id": req.trace_id or "auto"}
    finally:
        await _capacity_release()


# ---------------------------------------------------------------------------
# Legacy metrics / state passthrough
# ---------------------------------------------------------------------------

# Wire in the lightweight EVAL_STATE router so existing Cockpit panels
# (metrics, positions, orders) still work exactly as before.
try:
    from evaluator.routers.api import router as _state_router
    app.include_router(_state_router)
except Exception as e:  # pragma: no cover – useful even if router missing
    LOG.warning("Unable to include evaluator.routers.api: %s", e)


# ---------------------------------------------------------------------------
# Historical DB driver + API
# ---------------------------------------------------------------------------

from datetime import datetime
from typing import Any

try:
    # Prefer the shared DB reader utilities so we are in sync with DataHub
    from common.dbLayer import db_reader as _db_read
except Exception:  # pragma: no cover - allows unit tests to stub this
    _db_read = None  # type: ignore


class HistoricalDBDriver:
    """Thin wrapper around common.dbLayer.db_reader for ad-hoc scans.

    This is *not* a full replay engine – it just gives Evaluator (and
    EvalView) a simple way to ask: "what did the world look like for
    this symbol over this window?".

    Later we will swap this out for a proper Replay DataHub that feeds
    the same bus message shapes as LIVE mode.
    """

    def __init__(self, symbol: str, start: datetime, end: datetime):
        self.symbol = symbol.upper()
        self.start = start
        self.end = end
        if self.start >= self.end:
            raise ValueError("start must be before end")

        if _db_read is None:
            raise RuntimeError("common.dbLayer.db_reader is not available")

    def daily(self) -> list[dict[str, Any]]:
        rows = _db_read.get_daily_bars(self.symbol, self.start.date(), self.end.date())
        return [_to_mapping(r) for r in rows]

    def minute(self, limit: int | None = None) -> list[dict[str, Any]]:
        rows = _db_read.get_minute_bars(self.symbol, self.start, self.end, limit=limit)
        return [_to_mapping(r) for r in rows]

    def ticks(self, limit: int | None = None) -> list[dict[str, Any]]:
        rows = _db_read.get_ticks(self.symbol, self.start, self.end, limit=limit)
        return [_to_mapping(r) for r in rows]


def _to_mapping(obj: Any) -> dict[str, Any]:
    """Best-effort conversion of DB row objects to plain dicts.

    Handles:
      • plain dicts
      • dataclasses / simple objects with __dict__
      • namedtuples with _asdict()
    """
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "_asdict"):
        return dict(obj._asdict())  # type: ignore[arg-type]
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return {"value": repr(obj)}


class HistoricalScanRequest(BaseModel):
    """Request payload for a quick historical preview.

    This is intentionally narrow: one symbol, a UTC window, and simple
    toggles for which tiers to include.
    """

    symbol: Symbol
    start_utc: datetime
    end_utc: datetime
    include_daily: bool = True
    include_minute: bool = True
    include_ticks: bool = False
    tick_limit: Optional[int] = Field(default=5000, ge=1, le=500000)

    model_config = ConfigDict(extra="forbid")

    @field_validator("end_utc")
    @classmethod
    def _end_after_start(cls, v: datetime, info):
        start = info.data.get("start_utc")
        if start and v <= start:
            raise ValueError("end_utc must be after start_utc")
        return v


@app.post("/v1/historical/preview")
async def historical_preview(req: HistoricalScanRequest):
    """Lightweight historical data fetch for a single symbol.

    This is primarily a tooling endpoint for EvalView and for ad-hoc
    operator sanity checks while we bring the full Replay stack online.
    """
    drv = HistoricalDBDriver(str(req.symbol), req.start_utc, req.end_utc)

    out: dict[str, Any] = {"symbol": str(req.symbol)}

    if req.include_daily:
        out["daily"] = drv.daily()
    if req.include_minute:
        out["minute"] = drv.minute()
    if req.include_ticks:
        out["ticks"] = drv.ticks(limit=req.tick_limit)

    return {
        "ok": True,
        "window": {"start_utc": req.start_utc, "end_utc": req.end_utc},
        "data": out,
    }


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _on_startup() -> None:
    global R
    R = redis.from_url(_redis_url(), decode_responses=True)
    LOG.info("Registered routes: %s", [getattr(r, "path", str(r)) for r in app.routes])
    LOG.info("Evaluator ready")


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    global R
    if R:
        await R.aclose()
        R = None


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("evaluator.app:app", host="0.0.0.0", port=int(os.getenv("EVAL_PORT", "7001")))
