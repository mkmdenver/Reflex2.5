"""
BrokerView FastAPI backend.

Acts as a thin, opinionated proxy between the browser UI and the Trader API.
 - Handles session / market-state display
 - Exposes a normalized account overview for the UI
 - Proxies order placement and account actions (flatten / cancel-all)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from .market_session import current_session
from .models import (
    MarketSessionDoc,
    PositionsResponse,
    PositionDoc,
    OrdersResponse,
    OrderDoc,
    AccountsResponse,
    AccountSummary,
    PortfolioOverview,
)

log = logging.getLogger("brokerview")


def _env(name: str, default: Optional[str] = None) -> str:
    val = os.getenv(name, default)
    if val is None:
        raise RuntimeError(f"Missing required env var {name}")
    return val


BROKERVIEW_PORT = int(_env("BROKERVIEW_PORT", "7010"))
TRADER_API_PORT = int(_env("TRADER_API_PORT", "7002"))
TRADER_BASE = _env("TRADER_BASE", f"http://127.0.0.1:{TRADER_API_PORT}")

# ROOT for static/templates
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

log.info("BrokerView config port=%s trader_base=%s root=%s", BROKERVIEW_PORT, TRADER_BASE, ROOT)


class OrderIn(BaseModel):
    """
    Order entry payload accepted from the UI and forwarded to Trader.

    This is intentionally close to Trader's expected schema.
    """

    account_id: str
    symbol: str
    side: str  # "buy" | "sell"
    type: str  # "market" | "limit" | "stop" | "stop_limit"
    qty: float

    time_in_force: str = Field(alias="time_in_force")  # "day" | "gtc" | ...
    limit_price: Optional[float] = Field(default=None, alias="limit_price")
    stop_price: Optional[float] = Field(default=None, alias="stop_price")
    extended_hours: bool = Field(default=False, alias="extended_hours")

    note: Optional[str] = None
    exec_at: Optional[datetime] = None  # optional scheduled exec time

    class Config:
        populate_by_name = True


app = FastAPI(title="BrokerView")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # dev-friendly; tighten later if needed
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static assets (built React bundle)
static_dir = os.path.join(ROOT, "cockpit", "brokerview", "templates", "dist", "assets")
if os.path.isdir(static_dir):
    app.mount("/assets", StaticFiles(directory=static_dir), name="assets")


@app.get("/v1/time")
async def time_now() -> Dict[str, Any]:
    """
    Return authoritative time for UI sync.

    Truth chain:
      Trader (/v1/time) -> BrokerView proxy -> UI offset
    """
    try:
        async with httpx.AsyncClient(timeout=5) as cli:
            r = await cli.get(f"{TRADER_BASE}/v1/time")
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and "source" not in data:
                data["source"] = "trader"
            return data
    except Exception as e:
        # Fallback: admit we are local (don’t lie)
        now = datetime.now(timezone.utc)
        return {
            "ok": True,
            "server_utc_ms": int(now.timestamp() * 1000),
            "server_iso": now.isoformat().replace("+00:00", "Z"),
            "source": "brokerview_local_fallback",
            "stale": True,
            "error": repr(e),
        }


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    index_html = os.path.join(ROOT, "cockpit", "brokerview", "templates", "dist", "index.html")
    with open(index_html, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/v1/market/session", response_model=MarketSessionDoc)
async def market_session() -> MarketSessionDoc:
    raw = await current_session()

    raw_state = str(raw.get("state", "")).strip()
    raw_state_upper = raw_state.upper()

    if raw_state_upper in ("OPEN", "REGULAR", "REGULAR_TRADING"):
        state = "regular"
    elif raw_state_upper in ("CLOSED", "CLOSE"):
        state = "closed"
    elif raw_state_upper in ("PREMARKET", "PRE", "PRE_MARKET"):
        state = "premarket"
    elif raw_state_upper in ("POSTMARKET", "POST", "AFTER_HOURS", "AFTERHOURS"):
        state = "postmarket"
    else:
        state = "closed"

    # Prefer explicit "now" from upstream, else current UTC
    now_val = raw.get("now") or raw.get("server_time")
    if isinstance(now_val, str):
        try:
            cleaned = now_val.replace("Z", "+00:00") if "Z" in now_val and "+" not in now_val else now_val
            now_dt = datetime.fromisoformat(cleaned)
            if now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=timezone.utc)
        except Exception:
            now_dt = datetime.now(timezone.utc)
    elif now_val is not None:
        now_dt = now_val
    else:
        now_dt = datetime.now(timezone.utc)

    return MarketSessionDoc(
        state=state,
        now=now_dt,
        regular_open=raw.get("regular_open"),
        regular_close=raw.get("regular_close"),
        note=raw.get("note"),
    )


@app.get("/v1/accounts", response_model=AccountsResponse)
async def accounts() -> AccountsResponse:
    """
    UI-facing account overview.
    Calls Trader’s /v1/portfolio/overview and normalizes into [AccountSummary].
    """
    url = f"{TRADER_BASE}/v1/portfolio/overview"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            log.exception("Error fetching accounts overview from Trader: %s", e)
            raise HTTPException(status_code=502, detail="Trader unavailable")

        raw = resp.json()
        overview = PortfolioOverview(**raw)

        out: List[AccountSummary] = []
        for account_id, ov in overview.overview.items():
            balances = ov.balances
            cash = balances.cash or 0.0
            equity = balances.equity or 0.0
            buying_power = balances.buying_power or 0.0

            broker, _, acct_type = account_id.partition(":")
            broker = broker or "unknown"
            acct_type = acct_type or "unknown"

            label = f"{broker.capitalize()} {acct_type.capitalize()}"
            status = "ACTIVE"
            if broker == "sim":
                status = "SIM"
            elif broker == "alpaca":
                status = "LIVE"

            out.append(
                AccountSummary(
                    account_id=account_id,
                    label=label,
                    broker=broker,
                    account_type=acct_type,
                    status=status,
                    currency="USD",
                    cash=cash,
                    equity=equity,
                    buying_power=buying_power,
                    updated_at=balances.updated_at,
                )
            )

        return AccountsResponse(accounts=out)


@app.get("/v1/positions", response_model=PositionsResponse)
async def positions(account: Optional[str] = None) -> PositionsResponse:
    """
    Proxy positions from Trader.
    UI sends ?account=<account_id>; Trader expects ?account_id=<account_id>.
    """
    params: Dict[str, Any] = {}
    if account:
        params["account_id"] = account

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(f"{TRADER_BASE}/v1/portfolio/positions", params=params)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            log.exception("Error fetching positions from Trader: %s", e)
            raise HTTPException(status_code=502, detail="Trader unavailable")

        raw = resp.json()
        docs: List[PositionDoc] = []
        for p in raw.get("positions", []):
            qty = p.get("qty") or 0.0
            side = p.get("side")
            if side is None:
                if qty > 0:
                    side = "long"
                elif qty < 0:
                    side = "short"

            docs.append(
                PositionDoc(
                    account_id=p.get("account_id", ""),
                    symbol=p.get("symbol", ""),
                    qty=qty,
                    avg_price=p.get("avg_price") or 0.0,
                    market_price=p.get("market_price"),
                    market_value=p.get("market_value"),
                    unrealized_pl=p.get("unrealized_pl"),
                    unrealized_plpc=p.get("unrealized_plpc"),
                    realized_pl=p.get("realized_pl"),
                    side=side,
                    raw=p,
                )
            )

        return PositionsResponse(positions=docs)


@app.get("/v1/orders", response_model=OrdersResponse)
async def orders(status: str = "all", account: Optional[str] = None) -> OrdersResponse:
    """
    Proxy orders from Trader and normalize into OrderDoc.

    Supports:
      - active
      - closed
      - all (active + closed)

    IMPORTANT: passes selected account to Trader so it doesn't scan all accounts.
    """
    want_all = str(status or "").lower() == "all"
    params_base: Dict[str, Any] = {}
    if account:
        params_base["account_id"] = account

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            if want_all:
                p1 = dict(params_base, status="active")
                p2 = dict(params_base, status="closed")
                r1 = await client.get(f"{TRADER_BASE}/v1/orders", params=p1)
                r2 = await client.get(f"{TRADER_BASE}/v1/orders", params=p2)
                r1.raise_for_status()
                r2.raise_for_status()
                raw_orders = (r1.json().get("orders") or []) + (r2.json().get("orders") or [])
            else:
                p = dict(params_base, status=status)
                resp = await client.get(f"{TRADER_BASE}/v1/orders", params=p)
                resp.raise_for_status()
                raw_orders = resp.json().get("orders") or []
        except httpx.HTTPError as e:
            log.exception("Error fetching orders from Trader: %s", e)
            raise HTTPException(status_code=502, detail="Trader unavailable")

    docs: List[OrderDoc] = []
    for o in raw_orders:
        docs.append(
            OrderDoc(
                account_id=o.get("account_id", ""),
                symbol=o.get("symbol", ""),
                side=o.get("side"),
                status=o.get("status"),
                type=o.get("type"),
                qty=o.get("qty"),
                filled_qty=o.get("filled_qty"),
                limit_price=o.get("limit_price"),
                stop_price=o.get("stop_price"),
                time_in_force=o.get("time_in_force"),
                submitted_at=o.get("submitted_at"),
                updated_at=o.get("updated_at"),
                broker_order_id=o.get("broker_order_id") or o.get("id"),
                client_order_id=o.get("client_order_id"),
                raw=o,
            )
        )

    return OrdersResponse(orders=docs)


@app.post("/v1/orders")
async def place_order(order: OrderIn) -> Dict[str, Any]:
    payload = order.model_dump(by_alias=True)
    payload["note"] = payload.get("note") or ""

    url = f"{TRADER_BASE}/v1/orders/place"
    async with httpx.AsyncClient(timeout=20) as cli:
        try:
            resp = await cli.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
        except httpx.HTTPError as e:
            log.exception("Error talking to Trader /v1/orders/place: %s", e)
            raise HTTPException(status_code=502, detail="Trader unavailable")


class AccountActionIn(BaseModel):
    account_id: str


@app.post("/v1/accounts/flatten")
async def flatten_account(body: AccountActionIn) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as cli:
        try:
            resp = await cli.post(f"{TRADER_BASE}/v1/accounts/flatten", json={"account_id": body.account_id})
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            log.exception("Error flattening account via Trader: %s", e)
            raise HTTPException(status_code=502, detail="Trader unavailable")


@app.post("/v1/accounts/cancel_all")
async def cancel_all(body: AccountActionIn) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as cli:
        try:
            resp = await cli.post(f"{TRADER_BASE}/v1/accounts/cancel_all", json={"account_id": body.account_id})
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            log.exception("Error cancel_all via Trader: %s", e)
            raise HTTPException(status_code=502, detail="Trader unavailable")


@app.get("/events")
async def proxy_events(request: Request):
    """Proxy Trader SSE stream to the browser (avoids CORS issues)."""
    url = f"{TRADER_BASE}/v1/events"

    async def _iter():
        async with httpx.AsyncClient(timeout=None) as cli:
            async with cli.stream("GET", url, headers={"Accept": "text/event-stream"}) as r:
                async for chunk in r.aiter_raw():
                    if await request.is_disconnected():
                        break
                    yield chunk

    return StreamingResponse(_iter(), media_type="text/event-stream")
