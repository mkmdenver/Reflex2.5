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

    Preferred source: Trader’s /v1/accounts (stable, list-based).
    Fallback: Trader’s /v1/portfolio/overview (older shape).
    """
    async with httpx.AsyncClient(timeout=10) as client:
        # 1) Preferred: /v1/accounts
        try:
            resp = await client.get(f"{TRADER_BASE}/v1/accounts")
            resp.raise_for_status()
            raw = resp.json()
            items = raw.get("items") or []
        except httpx.HTTPError:
            # 2) Fallback: /v1/portfolio/overview (shape has drifted over time)
            try:
                resp = await client.get(f"{TRADER_BASE}/v1/portfolio/overview")
                resp.raise_for_status()
                raw = resp.json()
                accounts_map = raw.get("accounts") or raw.get("overview") or {}
                if isinstance(accounts_map, dict):
                    items = list(accounts_map.values())
                elif isinstance(accounts_map, list):
                    items = accounts_map
                else:
                    items = []
            except httpx.HTTPError as e:
                log.exception("Error fetching accounts from Trader: %s", e)
                raise HTTPException(status_code=502, detail="Trader unavailable")

    out: List[AccountSummary] = []
    for it in items:
        if not isinstance(it, dict):
            continue

        account_id = str(it.get("account_id") or "")
        if not account_id:
            continue

        broker = str(it.get("broker_id") or "").strip() or account_id.split(":", 1)[0]
        acct_type = account_id.split(":", 1)[1] if ":" in account_id and len(account_id.split(":", 1)) == 2 else "unknown"

        cash = float(it.get("cash") or 0.0)
        equity = float(it.get("equity") or 0.0)
        buying_power = float(it.get("buying_power") or 0.0)
        updated_at = it.get("updated_at")

        label = str(it.get("label") or "").strip() or account_id

        status = "ACTIVE"
        if broker == "sim":
            status = "SIM"
        elif broker == "alpaca":
            status = "PAPER" if acct_type == "paper" else "LIVE"

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
                updated_at=updated_at,
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

        # Trader returns {"items":[...], "account_id":"alpaca:paper"} but items often lack account_id.
        top_account_id = raw.get("account_id") or (account or "")

        docs: List[PositionDoc] = []
        raw_positions = raw.get("positions") or raw.get("items") or []
        for p in raw_positions:
            qty = p.get("qty") or 0.0
            side = p.get("side")
            if side is None:
                if qty > 0:
                    side = "long"
                elif qty < 0:
                    side = "short"

            docs.append(
                PositionDoc(
                    account_id=p.get("account_id") or top_account_id,
                    symbol=p.get("symbol", ""),
                    qty=qty,
                    avg_price=p.get("avg_price") or 0.0,
                    market_price=p.get("market_price"),
                    market_value=p.get("market_value"),
                    unrealized_pl=p.get("unrealized_pl"),
                    unrealized_plpc=p.get("unrealized_plpc"),
                    realized_pl=p.get("realized_pl"),
                    side=side,
                    stop_price=p.get("stop_price"),
                    target_price=p.get("target_price"),
                    take_profit_price=p.get("take_profit_price"),
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
                j1 = r1.json()
                j2 = r2.json()
                raw_orders = (j1.get("orders") or j1.get("items") or []) + (j2.get("orders") or j2.get("items") or [])
            else:
                p = dict(params_base, status=status)
                resp = await client.get(f"{TRADER_BASE}/v1/orders", params=p)
                resp.raise_for_status()
                j = resp.json()
                raw_orders = j.get("orders") or j.get("items") or []
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
@app.post("/v1/orders/place")
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


class PositionActionIn(BaseModel):
    account_id: str
    symbol: str


@app.post("/v1/positions/flatten")
async def flatten_one(body: PositionActionIn) -> Dict[str, Any]:
    """Flatten a single position symbol (best-effort).

    Notes:
    - Trader currently provides account-level flatten/cancel endpoints.
    - For live testing, this endpoint submits a market order sized to the
      broker position qty for this symbol.
    - Any existing active orders for the symbol are NOT cancelled (Trader has
      no per-order cancel endpoint today). The UI warns about this.
    """

    aid = (body.account_id or "").strip()
    sym = (body.symbol or "").strip().upper()
    if not aid or not sym:
        raise HTTPException(status_code=400, detail="account_id and symbol are required")

    async with httpx.AsyncClient(timeout=20) as cli:
        # Fetch current position from Trader (source of truth for qty/side)
        try:
            resp = await cli.get(f"{TRADER_BASE}/v1/portfolio/positions", params={"account_id": aid})
            resp.raise_for_status()
            items = resp.json().get("positions") or []
        except httpx.HTTPError as e:
            log.exception("Error fetching positions from Trader for flatten_one: %s", e)
            raise HTTPException(status_code=502, detail="Trader unavailable")

        pos = next((p for p in items if str(p.get("symbol", "")).upper() == sym), None)
        if not pos:
            return {"ok": False, "error": f"No open position for {sym}", "account_id": aid, "symbol": sym}

        qty = float(pos.get("qty") or 0)
        if qty <= 0:
            return {"ok": False, "error": f"Position qty not positive for {sym}", "account_id": aid, "symbol": sym}

        side = str(pos.get("side") or "long").lower()
        order_side = "sell" if side != "short" else "buy"

        payload = {
            "account_id": aid,
            "symbol": sym,
            "side": order_side,
            "type": "market",
            "qty": qty,
            "time_in_force": "day",
            "note": f"flatten_one:{sym}",
        }

        try:
            r2 = await cli.post(f"{TRADER_BASE}/v1/orders/place", json=payload)
            if r2.status_code >= 400:
                raise HTTPException(status_code=r2.status_code, detail=r2.text)
            return {"ok": True, "account_id": aid, "symbol": sym, "submitted": r2.json()}
        except HTTPException:
            raise
        except httpx.HTTPError as e:
            log.exception("Error placing flatten_one via Trader: %s", e)
            raise HTTPException(status_code=502, detail="Trader unavailable")


@app.post("/v1/accounts/flatten")
async def flatten_account(body: AccountActionIn) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as cli:
        try:
            resp = await cli.post(f"{TRADER_BASE}/v1/accounts/flatten", json={"account_id": body.account_id})
            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code, detail=resp.text)
            return resp.json()
        except httpx.ConnectError as e:
            log.exception("Error flattening account via Trader (connect): %s", e)
            raise HTTPException(status_code=502, detail="Trader unavailable")
        except httpx.ReadTimeout as e:
            log.exception("Error flattening account via Trader (timeout): %s", e)
            raise HTTPException(status_code=502, detail="Trader timeout")
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