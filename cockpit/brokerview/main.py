"""
BrokerView FastAPI backend.

Acts as a thin, opinionated proxy between the browser UI and the Trader API.
 - Handles session / market-state display
 - Exposes a normalized account overview for the UI
 - Proxies order placement and account actions (flatten / cancel-all)
"""

from __future__ import annotations

import asyncio

import logging
import os
import time
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


# ---------------------------------------------------------------------------
# Soft-cache to eliminate UI error flashes
# On upstream failure (Trader busy/restarting), return last-known-good snapshots.
# ---------------------------------------------------------------------------

# HARD RULE: BrokerView never invents state. Cache is only "last known good".
_LAST_ACCOUNTS: Optional[AccountsResponse] = None
_LAST_POSITIONS: Optional[PositionsResponse] = None
_LAST_ORDERS: Optional[OrdersResponse] = None

_OK_SINCE: Dict[str, float] = {}
_LAST_ERR: Dict[str, str] = {}


def _mark_ok(kind: str) -> None:
    _OK_SINCE[kind] = time.time()
    _LAST_ERR.pop(kind, None)


def _mark_err(kind: str, err: Exception) -> None:
    _LAST_ERR[kind] = repr(err)


def _get_cached(kind: str):
    if kind == "accounts":
        return _LAST_ACCOUNTS
    if kind == "positions":
        return _LAST_POSITIONS
    if kind == "orders":
        return _LAST_ORDERS
    return None


def _model_allowed_keys(model_cls) -> set[str]:
    # Pydantic v2: model_fields; v1: __fields__
    mf = getattr(model_cls, "model_fields", None)
    if isinstance(mf, dict):
        return set(mf.keys())
    ff = getattr(model_cls, "__fields__", None)
    if isinstance(ff, dict):
        return set(ff.keys())
    return set()


def _build_doc(model_cls, payload: Dict[str, Any]):
    # Filter payload to model fields to avoid validation errors / strict models
    allowed = _model_allowed_keys(model_cls)
    if allowed:
        payload = {k: v for k, v in payload.items() if k in allowed}
    try:
        # pydantic v2
        mv = getattr(model_cls, "model_validate", None)
        if callable(mv):
            return mv(payload)
    except Exception:
        pass
    return model_cls(**payload)


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
                return AccountsResponse(items=[])

                return AccountsResponse(items=[])

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

    Behavior: on transient Trader failures, return last-known-good positions
    (or empty). This keeps the UI stable during refresh bursts.
    """
    params: Dict[str, Any] = {}
    if account:
        params["account_id"] = account

    async with httpx.AsyncClient(timeout=5) as client:
        try:
            resp = await client.get(f"{TRADER_BASE}/v1/portfolio/positions", params=params)
            resp.raise_for_status()
            raw = resp.json() if resp.content else {}
        except Exception as e:
            _mark_err("positions", e)
            cached = _get_cached("positions")
            if cached is not None:
                return cached
            return PositionsResponse(positions=[])

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
        try:
            payload = {
                "account_id": p.get("account_id") or top_account_id,
                "symbol": p.get("symbol", ""),
                "qty": qty,
                "avg_price": p.get("avg_price") or p.get("avg_entry_price") or 0.0,
                "market_value": p.get("market_value"),
                "unrealized_pl": p.get("unrealized_pl"),
                "stop_price": p.get("stop_price"),
                "target_price": p.get("target_price"),
                "close_reason": p.get("close_reason"),
                "gen_id": p.get("gen_id"),
                "side": side,
                "raw": p,
            }
            docs.append(_build_doc(PositionDoc, payload))
        except Exception:
            # Skip malformed rows; never fail the whole endpoint
            continue

    out = PositionsResponse(positions=docs)
    global _LAST_POSITIONS
    _LAST_POSITIONS = out
    _mark_ok("positions")
    return out



@app.get("/v1/orders", response_model=OrdersResponse)
async def orders(account: Optional[str] = None, status: Optional[str] = None) -> OrdersResponse:
    """
    Proxy orders from Trader.

    Behavior: on transient Trader failures, return last-known-good orders
    (or empty) to eliminate scary UI flashes.
    """
    params: Dict[str, Any] = {}
    if account:
        params["account_id"] = account
    if status:
        params["status"] = status

    async with httpx.AsyncClient(timeout=5) as client:
        try:
            resp = await client.get(f"{TRADER_BASE}/v1/orders", params=params)
            resp.raise_for_status()
            raw = resp.json() if resp.content else {}
        except Exception as e:
            _mark_err("orders", e)
            cached = _get_cached("orders")
            if cached is not None:
                return cached
            return OrdersResponse(orders=[])

    items = raw.get("items") or raw.get("orders") or []
    docs: List[OrderDoc] = []
    for o in items:
        try:
            payload = {
                "account_id": o.get("account_id") or (account or ""),
                "id": str(o.get("id") or o.get("order_id") or ""),
                "symbol": o.get("symbol") or "",
                "side": o.get("side") or "",
                "type": o.get("type") or "",
                "qty": float(o.get("qty") or o.get("quantity") or 0.0),
                "filled_qty": float(o.get("filled_qty") or o.get("filled_quantity") or 0.0),
                "limit_price": o.get("limit_price"),
                "stop_price": o.get("stop_price"),
                "time_in_force": o.get("time_in_force") or o.get("tif"),
                "status": o.get("status") or "",
                "submitted_at": o.get("submitted_at"),
                "filled_at": o.get("filled_at"),
                "raw": o,
            }
            docs.append(_build_doc(OrderDoc, payload))
        except Exception:
            continue

    out = OrdersResponse(orders=docs)
    global _LAST_ORDERS
    _LAST_ORDERS = out
    _mark_ok("orders")
    return out


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
    """Proxy Trader SSE stream to the browser (avoids CORS issues).

    HARD RULE: never crash BrokerView. If Trader is down, emit a lightweight
    status event and keep the stream alive.
    """
    trader_base = TRADER_BASE.rstrip("/")
    url = f"{trader_base}/events"

    async def _iter():
        while True:
            if await request.is_disconnected():
                break
            try:
                async with httpx.AsyncClient(timeout=10) as cli:
                    async with cli.stream("GET", url, headers={"Accept": "text/event-stream"}) as r:
                        async for chunk in r.aiter_raw():
                            if chunk:
                                yield chunk
                await asyncio.sleep(0.25)
            except Exception:
                yield b"event: status\ndata: trader_unreachable\n\n"
                await asyncio.sleep(1.0)

    return StreamingResponse(_iter(), media_type="text/event-stream")



@app.get("/v1/closed_positions")
async def closed_positions(scope: str = "today", account: Optional[str] = None, limit: int = 1000) -> Dict[str, Any]:
    """Closed positions list for UI.

    HARD RULE: If Trader is unreachable OR endpoint is missing, return empty.
    BrokerView never invents state.
    """
    params: Dict[str, Any] = {"scope": scope, "limit": int(limit)}
    if account:
        params["account_id"] = account

    paths = [
        "/v1/portfolio/closed_positions",
        "/v1/portfolio/closed-positions",
        "/v1/closed_positions",
        "/v1/closed-positions",
    ]

    async with httpx.AsyncClient(timeout=5) as client:
        for p in paths:
            try:
                resp = await client.get(f"{TRADER_BASE}{p}", params=params)
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                data = resp.json() if resp.content else {}
                items = data.get("items") or data.get("closed_positions") or []
                return {"closed_positions": items}
            except Exception:
                continue

    return {"closed_positions": []}
