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
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

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

# ---------------------------------------------------------------------------
# Simple env / config
# ---------------------------------------------------------------------------


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

log.info(
    "BrokerView config port=%s trader_base=%s root=%s",
    BROKERVIEW_PORT,
    TRADER_BASE,
    ROOT,
)

# ---------------------------------------------------------------------------
# Order payload going *to* Trader
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# FastAPI app + CORS + static
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# API: Time (used by UI clock sync)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# API: Time (used by UI clock sync) — proxy Trader/DataHub authority
# ---------------------------------------------------------------------------

@app.get("/v1/time")
async def time_now() -> Dict[str, Any]:
    """
    Return authoritative time for UI sync.

    Desired truth chain:
      DataHub (authoritative tick) -> Trader cache (/v1/time) -> BrokerView proxy -> UI offset
    """
    # Try Trader first (authoritative)
    try:
        async with httpx.AsyncClient(timeout=5) as cli:
            r = await cli.get(f"{TRADER_BASE}/v1/time")
            r.raise_for_status()
            data = r.json()
            # Ensure a source tag is present for UI display/debug
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


# ---------------------------------------------------------------------------
# HTML root – serve the SPA shell
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    index_html = os.path.join(
        ROOT, "cockpit", "brokerview", "templates", "dist", "index.html"
    )
    with open(index_html, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# API: Market session
# ---------------------------------------------------------------------------

@app.get("/v1/market/session", response_model=MarketSessionDoc)
async def market_session() -> MarketSessionDoc:
    """
    Normalize raw session info from Trader into the schema expected by
    MarketSessionDoc. Trader / exchange libs may report states like
    "OPEN"/"CLOSED" etc; the UI model only knows:
        - "closed"
        - "premarket"
        - "regular"
        - "postmarket"
    and requires a "now" field.
    """
    raw = await current_session()

    # Defensive: allow both upper/lower/mixed state strings from upstream
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
        # If we ever see an unknown state, fall back to "closed"
        state = "closed"

    # Prefer an explicit "now"/"server_time" from upstream; otherwise use current UTC
    now_val = raw.get("now") or raw.get("server_time")
    if isinstance(now_val, str):
        try:
            from datetime import datetime, timezone

            # Handle ISO8601 with or without "Z"
            cleaned = now_val.replace("Z", "+00:00") if "Z" in now_val and "+" not in now_val else now_val
            now_dt = datetime.fromisoformat(cleaned)
            if now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=timezone.utc)
        except Exception:
            from datetime import datetime, timezone

            now_dt = datetime.now(timezone.utc)
    elif now_val is not None:
        now_dt = now_val
    else:
        from datetime import datetime, timezone

        now_dt = datetime.now(timezone.utc)

    # MarketSessionDoc likely ignores extra fields, so we pass through anything
    # useful like regular_open / regular_close / note.
    return MarketSessionDoc(
        state=state,
        now=now_dt,
        regular_open=raw.get("regular_open"),
        regular_close=raw.get("regular_close"),
        note=raw.get("note"),
    )



# ---------------------------------------------------------------------------
# API: Accounts + overview
# ---------------------------------------------------------------------------


@app.get("/v1/accounts", response_model=AccountsResponse)
async def accounts() -> AccountsResponse:
    """
    UI-facing account overview.

    We call Trader’s /v1/portfolio/overview and normalize into
    a simple [AccountSummary] list so the React side can stay dumb and happy.
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

        accounts: List[AccountSummary] = []
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

            accounts.append(
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

        return AccountsResponse(accounts=accounts)


# ---------------------------------------------------------------------------
# API: Positions – proxied from Trader
# ---------------------------------------------------------------------------


@app.get("/v1/positions", response_model=PositionsResponse)
async def positions(account: Optional[str] = None) -> PositionsResponse:
    """Proxy positions from Trader.

    The UI sends `?account=<account_id>` (historical naming).
    Trader expects `?account_id=<account_id>`.
    """
    params = {}
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


# ---------------------------------------------------------------------------
# API: Orders – proxied from Trader
# ---------------------------------------------------------------------------


@app.get("/v1/orders", response_model=OrdersResponse)
async def orders(status: str = "all") -> OrdersResponse:
    """
    Proxy active/closed orders from Trader and normalize into OrderDoc.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(f"{TRADER_BASE}/v1/orders", params={"status": status})
            resp.raise_for_status()
        except httpx.HTTPError as e:
            log.exception("Error fetching orders from Trader: %s", e)
            raise HTTPException(status_code=502, detail="Trader unavailable")

        raw = resp.json()
        docs: List[OrderDoc] = []
        for o in raw.get("orders", []):
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


# ---------------------------------------------------------------------------
# API: Place order – UI → BrokerView → Trader
# ---------------------------------------------------------------------------


@app.post("/v1/orders")
async def place_order(order: OrderIn) -> Dict[str, Any]:
    """
    Entry point for the React order ticket.

    - Normalize incoming order (symbol/qty/etc)
    - Forward to Trader `/v1/orders/place`
    - Bubble up any error text from Trader
    """
    payload = order.model_dump(by_alias=True)
    payload["note"] = payload.get("note") or ""

    log.info("BrokerView place_order -> Trader payload=%s", payload)

    url = f"{TRADER_BASE}/v1/orders/place"
    async with httpx.AsyncClient(timeout=20) as cli:
        try:
            resp = await cli.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            log.info("Trader accepted order: %s", data)
            return data
        except httpx.HTTPStatusError as e:
            text = e.response.text
            log.warning(
                "Trader rejected order status=%s body=%s",
                e.response.status_code,
                text,
            )
            raise HTTPException(status_code=e.response.status_code, detail=text)
        except httpx.HTTPError as e:
            log.exception("Error talking to Trader /v1/orders/place: %s", e)
            raise HTTPException(status_code=502, detail="Trader unavailable")


# ---------------------------------------------------------------------------
# API: Account-level actions – flatten / cancel all
# ---------------------------------------------------------------------------


class AccountActionIn(BaseModel):
    account_id: str


@app.post("/v1/accounts/flatten")
async def flatten_account(body: AccountActionIn) -> Dict[str, Any]:
    """
    Ask Trader to flatten all positions for an account.
    """
    async with httpx.AsyncClient(timeout=20) as cli:
        try:
            resp = await cli.post(
                f"{TRADER_BASE}/v1/accounts/flatten",
                json={"account_id": body.account_id},
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            log.exception("Error flattening account via Trader: %s", e)
            raise HTTPException(status_code=502, detail="Trader unavailable")


@app.post("/v1/accounts/cancel_all")
async def cancel_all(body: AccountActionIn) -> Dict[str, Any]:
    """
    Ask Trader to cancel all open orders for an account.
    """
    async with httpx.AsyncClient(timeout=20) as cli:
        try:
            resp = await cli.post(
                f"{TRADER_BASE}/v1/accounts/cancel_all",
                json={"account_id": body.account_id},
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            log.exception("Error cancel_all via Trader: %s", e)
            raise HTTPException(status_code=502, detail="Trader unavailable")
@app.get("/events")
async def proxy_events(request: Request):
    """Proxy Trader SSE stream to the browser (avoids CORS issues)."""
    url = f"{TRADER_BASE}/v1/events"
    async with httpx.AsyncClient(timeout=None) as cli:
        r = await cli.stream("GET", url, headers={"Accept": "text/event-stream"})

        async def _iter():
            async for chunk in r.aiter_raw():
                if await request.is_disconnected():
                    break
                yield chunk

        return StreamingResponse(_iter(), media_type="text/event-stream")



