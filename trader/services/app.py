# trader/app.py
# v2.1.2 – Trader HTTP API + telemetry plumbing (ACK/FILL instrumentation)
#
# v2.1.2:
#   - Add compatibility endpoints for BrokerView:
#       /v1/portfolio/overview
#       /v1/portfolio/positions?account_id=...
#       /v1/accounts
#   - Keep TradeRunner constructor signature fix (store/alerts/adapters/portfolio_manager).

import os
import time
import asyncio
import logging
import threading
from uuid import uuid4
from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import JSONResponse
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Deque
from collections import deque as _deque

from fastapi import FastAPI, Query, HTTPException

from .portfolio_manager import PortfolioManager
from .alpaca_trade_updates import listen_trade_updates
from .trade_runner import TradeRunner

log = logging.getLogger("trader.app")
app = FastAPI(title="Reflex Trader API", version="2.1.2")

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

pm = PortfolioManager()

def _env_present(*keys: str) -> bool:
    return any((os.getenv(k) or "").strip() for k in keys)

async def _register_adapters_once() -> None:
    """Register broker adapters before background loops start.

    If adapters are never registered, BrokerView will show an empty account
    dropdown and downstream endpoints become meaningless.
    """
    # 1) DB-backed registry (preferred when configured)
    try:
        fn = getattr(pm, "register_from_db", None)
        if fn is not None:
            await fn()
    except Exception:
        log.exception("startup.register_from_db.failed")

    # 2) Env fallback (restores the "worked yesterday" behavior when DB is empty)
    try:
        if not getattr(pm, "adapters", {}):
            # Alpaca
            if _env_present("ALPACA_API_KEY_ID", "ALPACA_API_KEY") and _env_present("ALPACA_API_SECRET_KEY", "ALPACA_API_SECRET"):
                from ..adapters.alpaca_adapter import AlpacaAdapter

                key_id = (os.getenv("ALPACA_API_KEY_ID") or os.getenv("ALPACA_API_KEY") or "").strip()
                secret = (os.getenv("ALPACA_API_SECRET_KEY") or os.getenv("ALPACA_API_SECRET") or "").strip()
                base = (os.getenv("ALPACA_BASE_URL") or "https://paper-api.alpaca.markets").strip()
                aid = (os.getenv("TRADER_DEFAULT_ACCOUNT_ID") or "alpaca:paper").strip()
                pm.adapters[aid] = AlpacaAdapter(account_id=aid, base=base, key_id=key_id, secret=secret)

            # SIM fallback
            if not getattr(pm, "adapters", {}):
                from ..portfolio_manager import SimAdapter

                sim_id = (os.getenv("TRADER_SIM_ACCOUNT_ID") or "sim:cash").strip()
                pm.adapters[sim_id] = SimAdapter(account_id=sim_id, starting_cash=float(os.getenv("TRADER_SIM_STARTING_CASH", "100000") or 100000), margin=False)
    except Exception:
        log.exception("startup.env_fallback.failed")

# ---------------------------------------------------------------------------
# Async safety – never block the event loop
# ---------------------------------------------------------------------------

async def _call_maybe_async(fn, *args, **kwargs):
    if fn is None:
        return None
    if asyncio.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    return await asyncio.to_thread(fn, *args, **kwargs)


def _default_account_id() -> str | None:
    try:
        if len(pm.adapters) == 1:
            return next(iter(pm.adapters.keys()))
        return None
    except Exception:
        return None

def _fail_local(client_id: str, aid: str, reason: str, status: str = "failed") -> None:
    try:
        o = _LOCAL_ORDERS.get(client_id)
        if not o:
            return
        o["status"] = status
        o["error"] = reason
        o["updated_at"] = _iso_now()
    except Exception:
        pass

def _broker_id_from_result(res):
    try:
        if isinstance(res, dict):
            return res.get("id") or res.get("order_id") or res.get("broker_order_id")
        return getattr(res, "id", None) or getattr(res, "order_id", None)
    except Exception:
        return None


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()

# ---------------------------------------------------------------------------
# Local Orders Ledger (cache)
# ---------------------------------------------------------------------------

_LOCAL_ORDERS: Dict[str, Dict[str, Any]] = {}     # key: broker id when known, else client id
_CLIENT_TO_BROKER: Dict[str, str] = {}            # client_id -> broker_id

def _is_closed_status(st: Optional[str]) -> bool:
    s = (st or "").lower()
    return s in ("filled", "canceled", "cancelled", "rejected", "expired", "done", "failed")

def _is_active_status(st: Optional[str]) -> bool:
    s = (st or "").lower()
    if not s:
        return True
    if _is_closed_status(s):
        return False
    return s in ("pending_local", "new", "accepted", "pending_new", "submitted", "partially_filled", "open")

# ---------------------------------------------------------------------------
# Managed Trades (in-memory; TradeRunner reads these through StoreShim)
# ---------------------------------------------------------------------------

_MANAGED_TRADES: Dict[str, Dict[str, Any]] = {}
_INTENT_HISTORY: Deque[Dict[str, Any]] = _deque(maxlen=500)

_LAST_ORDERS_SYNC_TS: float = 0.0
_LAST_ORDERS_SYNC_ERR: Optional[str] = None

# ---------------------------------------------------------------------------
# BrokerView helpers (entry price on exits so P/L can be computed)
# ---------------------------------------------------------------------------

def _avg_entry_price_for_order(o: Dict[str, Any]) -> Optional[float]:
    try:
        cid = str(o.get("client_order_id") or "")
        if ":exit:" not in cid or ":" not in cid:
            return None
        trade_id = cid.split(":", 1)[0]
        t = _MANAGED_TRADES.get(trade_id)
        if not isinstance(t, dict):
            return None
        px = t.get("entry_avg_price")
        if px is None:
            return None
        return float(px)
    except Exception:
        return None

def _local_order_doc(o: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "account_id": o.get("account_id"),
        "id": o.get("id"),
        "symbol": o.get("symbol"),
        "side": o.get("side"),
        "qty": float(o.get("qty") or 0),
        "type": o.get("type"),
        "limit_price": o.get("limit_price"),
        "stop_price": o.get("stop_price"),
        "time_in_force": o.get("time_in_force"),
        "status": o.get("status"),
        "submitted_at": o.get("submitted_at"),
        "updated_at": o.get("updated_at"),
        "client_order_id": o.get("client_order_id"),
        "avg_entry_price": _avg_entry_price_for_order(o),
        "broker_order_id": o.get("broker_order_id") or o.get("id"),
        # Telemetry fields (set by trade_updates)
        "ack_ts": o.get("ack_ts"),
        "first_fill_ts": o.get("first_fill_ts"),
        "first_fill_price": o.get("first_fill_price"),
        "filled_ts": o.get("filled_ts"),
        "avg_fill_price": o.get("avg_fill_price") or o.get("filled_avg_price"),
        "filled_qty": o.get("filled_qty"),
        "source": o.get("source"),
        "note": o.get("note"),
        "error": o.get("error"),
    }

def _expire_stale_pending_locals() -> None:
    try:
        now = time.time()
        kill: List[str] = []
        for k, o in list(_LOCAL_ORDERS.items()):
            if o.get("source") != "local":
                continue
            st = str(o.get("status") or "")
            if st not in ("pending_local", "pending_new", "submitted"):
                continue
            ts = float(o.get("created_ts") or 0.0)
            if ts and (now - ts) > 90.0:
                kill.append(k)
        for k in kill:
            _LOCAL_ORDERS.pop(k, None)
    except Exception:
        pass

def _get_local_order_by_any_id(broker_id: Optional[str], client_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if broker_id:
        o = _LOCAL_ORDERS.get(broker_id)
        if o:
            return o
    if client_id:
        o = _LOCAL_ORDERS.get(client_id)
        if o:
            return o
        mapped = _CLIENT_TO_BROKER.get(client_id)
        if mapped:
            o = _LOCAL_ORDERS.get(mapped)
            if o:
                return o
    return None

def _upsert_local_telemetry(aid: str, broker_id: Optional[str], client_id: Optional[str], patch: Dict[str, Any]) -> None:
    key = broker_id or client_id
    if not key:
        return
    existing = _get_local_order_by_any_id(broker_id, client_id) or {}
    merged = dict(existing)
    merged.update({k: v for k, v in patch.items() if v is not None})
    merged["account_id"] = aid
    if broker_id:
        merged["id"] = broker_id
    if client_id and not merged.get("client_order_id"):
        merged["client_order_id"] = client_id
    merged["updated_at"] = _iso_now()

    if broker_id:
        _LOCAL_ORDERS[broker_id] = merged
        if client_id:
            _CLIENT_TO_BROKER[client_id] = broker_id
            _LOCAL_ORDERS.pop(client_id, None)
    else:
        _LOCAL_ORDERS[client_id] = merged  # type: ignore[arg-type]

def _apply_trade_update_telemetry(account_id: str, evt: Dict[str, Any]) -> None:
    try:
        ev = str((evt or {}).get("event") or (evt or {}).get("type") or "").lower()
        o = (evt or {}).get("order") or {}
        if not isinstance(o, dict):
            o = {}
        broker_id = o.get("id") or o.get("order_id") or (evt or {}).get("order_id")
        client_id = o.get("client_order_id") or (evt or {}).get("client_order_id")
        now = time.time()

        filled_avg = o.get("filled_avg_price") or o.get("avg_fill_price") or o.get("average_fill_price")
        filled_qty = o.get("filled_qty") or o.get("filled_quantity") or (evt or {}).get("filled_qty") or (evt or {}).get("filled_quantity")
        exec_px = (evt or {}).get("price") or (evt or {}).get("execution_price") or (evt or {}).get("fill_price")

        patch: Dict[str, Any] = {
            "id": broker_id,
            "client_order_id": client_id,
            "symbol": o.get("symbol") or (evt or {}).get("symbol"),
            "side": o.get("side") or (evt or {}).get("side"),
            "qty": o.get("qty") or (evt or {}).get("qty"),
            "status": o.get("status") or ev,
        }

        if ev in ("new", "accepted", "pending_new", "submitted", "open"):
            patch["ack_ts"] = now

        if ev in ("partial_fill", "fill"):
            existing = _get_local_order_by_any_id(str(broker_id) if broker_id else None, str(client_id) if client_id else None) or {}
            if existing.get("first_fill_ts") is None:
                patch["first_fill_ts"] = now
                try:
                    patch["first_fill_price"] = float(exec_px or filled_avg) if (exec_px or filled_avg) is not None else None
                except Exception:
                    patch["first_fill_price"] = None

        if ev == "fill" or str((o.get("status") or "")).lower() == "filled":
            patch["filled_ts"] = now
            try:
                if filled_avg is not None:
                    patch["avg_fill_price"] = float(filled_avg)
            except Exception:
                pass
            try:
                if filled_qty is not None:
                    patch["filled_qty"] = float(filled_qty)
            except Exception:
                pass

        _upsert_local_telemetry(account_id, str(broker_id) if broker_id else None, str(client_id) if client_id else None, patch)

        # Mirror into _MANAGED_TRADES
        try:
            cid = str(client_id or "")
            if ":" in cid:
                trade_id = cid.split(":", 1)[0]
                t = _MANAGED_TRADES.get(trade_id)
                if isinstance(t, dict):
                    if ":entry" in cid:
                        if "ack_ts" in patch:
                            t["entry_ack_ts"] = patch["ack_ts"]
                        if "first_fill_ts" in patch and t.get("entry_first_fill_ts") is None:
                            t["entry_first_fill_ts"] = patch["first_fill_ts"]
                            t["entry_first_fill_price"] = patch.get("first_fill_price")
                        if "filled_ts" in patch:
                            t["entry_filled_ts"] = patch["filled_ts"]
                            if patch.get("avg_fill_price") is not None:
                                t["entry_fill_price"] = patch.get("avg_fill_price")
                    elif ":exit:" in cid:
                        if "ack_ts" in patch:
                            t["exit_ack_ts"] = patch["ack_ts"]
                        if "first_fill_ts" in patch and t.get("exit_first_fill_ts") is None:
                            t["exit_first_fill_ts"] = patch["first_fill_ts"]
                            t["exit_first_fill_price"] = patch.get("first_fill_price")
                        if "filled_ts" in patch:
                            t["exit_filled_ts"] = patch["filled_ts"]
                            t["exit_avg_fill_price"] = patch.get("avg_fill_price")
                            t["exit_filled_qty"] = patch.get("filled_qty")
                            if patch.get("avg_fill_price") is not None:
                                t["exit_fill_price"] = patch.get("avg_fill_price")
        except Exception:
            pass
    except Exception:
        return

# ---------------------------------------------------------------------------
# Store / Alerts shims for TradeRunner
# ---------------------------------------------------------------------------

class _StoreShim:
    def get_active_trades(self) -> List[Dict[str, Any]]:
        return list(_MANAGED_TRADES.values())

    def get_md(self, symbol: str) -> Dict[str, Any]:
        try:
            fn = getattr(pm, "get_md", None)
            if fn is not None:
                return fn(symbol) or {}
        except Exception:
            pass
        return {}

    def journal_append(self, kind: str, obj: Dict[str, Any]) -> None:
        return

class _AlertsShim:
    def emit(self, event_type: str, **evt: Any) -> None:
        return

_STORE = _StoreShim()
_ALERTS = _AlertsShim()


# ---------------------------------------------------------------------------
# Orders cache refresh (broker truth -> local echo)
# ---------------------------------------------------------------------------

async def _refresh_orders_cache_once() -> None:
    """Pull broker orders (open+closed) and update _LOCAL_ORDERS statuses.

    This prevents orders from getting stuck in SUBMITTED when fills occur.
    We match broker orders by broker order id using _CLIENT_TO_BROKER mapping.
    """
    global _LAST_ORDERS_SYNC_TS, _LAST_ORDERS_SYNC_ERR
    try:
        rev: Dict[str, str] = {str(bid): str(cid) for cid, bid in _CLIENT_TO_BROKER.items() if bid}
        updated = 0

        for aid, ad in list(getattr(pm, "adapters", {}).items()):
            # adapter must expose list_open_orders(status=...)
            lister = getattr(ad, "list_open_orders", None)
            if lister is None:
                continue
            for st in ("open", "closed"):
                try:
                    arr = await _call_maybe_async(lister, st)
                except Exception:
                    arr = []
                if not isinstance(arr, list):
                    continue
                for bo in arr:
                    if not isinstance(bo, dict):
                        continue
                    bid = bo.get("id") or bo.get("order_id")
                    if not bid:
                        continue
                    cid = rev.get(str(bid))
                    if not cid:
                        continue
                    lo = _LOCAL_ORDERS.get(cid)
                    if not lo:
                        continue
                    # update fields we commonly display
                    lo["broker_order_id"] = str(bid)
                    if bo.get("status"):
                        lo["status"] = str(bo.get("status"))
                    if bo.get("filled_qty") is not None:
                        lo["filled_qty"] = bo.get("filled_qty")
                    if bo.get("filled_avg_price") is not None:
                        lo["filled_avg_price"] = bo.get("filled_avg_price")
                    if bo.get("submitted_at") is not None:
                        lo.setdefault("submitted_at", bo.get("submitted_at"))
                    lo["updated_at"] = _iso_now()
                    updated += 1

        _LAST_ORDERS_SYNC_TS = time.time()
        _LAST_ORDERS_SYNC_ERR = None
        if updated:
            log.info("orders.sync.updated", extra={"updated": updated})
    except Exception as e:
        _LAST_ORDERS_SYNC_TS = time.time()
        _LAST_ORDERS_SYNC_ERR = str(e)
        log.exception("orders.sync.failed")


async def _background_orders_cache_refresh() -> None:
    while True:
        try:
            await _refresh_orders_cache_once()
        except Exception:
            pass
        await asyncio.sleep(float(os.getenv("TRADER_ORDERS_SYNC_SECS", "2") or 2))

# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

_TRADE_RUNNER: Optional[TradeRunner] = None
_bg_orders_task: Optional[asyncio.Task] = None
_bg_reconcile_task: Optional[asyncio.Task] = None
_bg_events_stop = asyncio.Event()
_MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None

async def _background_reconcile(loop: asyncio.AbstractEventLoop):
    while True:
        try:
            for aid in list(getattr(pm, "adapters", {}).keys()):
                fn = getattr(pm, "reconcile_account", None)
                if fn is not None:
                    await _call_maybe_async(fn, aid)
        except Exception:
            pass
        await asyncio.sleep(float(os.getenv("TRADER_RECONCILE_SECS", "5") or 5))

async def _background_broker_events(loop: asyncio.AbstractEventLoop):
    tasks: List[asyncio.Task] = []
    for aid, a in list(getattr(pm, "adapters", {}).items()):
        try:
            if getattr(a, "broker_id", "") != "alpaca":
                continue
            base = getattr(a, "base", None) or os.getenv("ALPACA_BASE_URL") or "https://paper-api.alpaca.markets"
            key_id = getattr(a, "key_id", None) or os.getenv("ALPACA_API_KEY") or ""
            secret = getattr(a, "secret", None) or os.getenv("ALPACA_API_SECRET") or ""
            if not key_id or not secret:
                continue

            def _on_evt_factory(account_id: str):
                def _on_evt(evt: Dict[str, Any]) -> None:
    _apply_trade_update_telemetry(account_id, evt)
    try:
        # record event for UI/debug if supported
        rec = getattr(pm, "record_broker_event", None)
        if rec is not None:
            loop.call_soon_threadsafe(asyncio.create_task, _call_maybe_async(rec, account_id, evt))
        # immediate reconcile on broker updates
        rec_acc = getattr(pm, "reconcile_account", None)
        if rec_acc is not None:
            loop.call_soon_threadsafe(asyncio.create_task, _call_maybe_async(rec_acc, account_id))
        # refresh orders cache so statuses advance (SUBMITTED->FILLED)
        loop.call_soon_threadsafe(asyncio.create_task, _refresh_orders_cache_once())
    except Exception:
        pass
                return _on_evt

            tasks.append(
                asyncio.create_task(
                    listen_trade_updates(
                        account_id=aid,
                        base_https=base,
                        key_id=key_id,
                        secret=secret,
                        on_event=_on_evt_factory(aid),
                        stop_event=_bg_events_stop,
                        stream_url=os.getenv("ALPACA_TRADE_UPDATES_URL"),
                    )
                )
            )
        except Exception:
            log.exception("broker_events.task_create.failed", extra={"account_id": aid})

    if tasks:
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass

def _ensure_trade_runner() -> TradeRunner:
    global _TRADE_RUNNER
    if _TRADE_RUNNER is None:
        _TRADE_RUNNER = TradeRunner(
            store=_STORE,
            alerts=_ALERTS,
            adapters=getattr(pm, "adapters", {}),
            portfolio_manager=pm,
        )
    return _TRADE_RUNNER

async def _background_trade_runner(loop: asyncio.AbstractEventLoop):
    tr = _ensure_trade_runner()
    await tr.run_forever()

@app.on_event("startup")
async def on_startup():
    global _MAIN_LOOP, _bg_reconcile_task, _bg_orders_task
    log.info("startup.begin")
    loop = asyncio.get_event_loop()
    _MAIN_LOOP = loop

    # Register adapters before background loops start; otherwise BrokerView
    # sees no accounts.
    await _register_adapters_once()

    # Prime snapshot for first UI poll.
    try:
        fn = getattr(pm, "reconcile_once", None)
        if fn is not None:
            await fn()
    except Exception:
        log.exception("startup.reconcile_once.failed")

    _bg_reconcile_task = asyncio.create_task(_background_reconcile(loop))
    _bg_orders_task = asyncio.create_task(_background_orders_cache_refresh())
    asyncio.create_task(_background_broker_events(loop))
    asyncio.create_task(_background_trade_runner(loop))
    log.info("startup.done")

@app.on_event("shutdown")
async def on_shutdown():
    try:
        _bg_events_stop.set()
    except Exception:
        pass
    try:
        if _bg_reconcile_task:
            _bg_reconcile_task.cancel()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# API Endpoints (compat with BrokerView)
# ---------------------------------------------------------------------------
@app.get("/v1/debug/exit_eval")
async def debug_exit_eval(
    account_id: Optional[str] = Query(None),
    trade_id: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
):
    """
    Debug endpoint: show the exact price inputs TradeRunner uses (lt/bid/ask),
    the derived last_px (lt->bid->ask), and whether stop/target would trigger.

    Uses the same store.get_md() path TradeRunner uses, so this verifies the
    DataHub tick/quote feed wiring end-to-end.
    """
    def _as_float(x):
        try:
            if x is None:
                return None
            if isinstance(x, bool):
                return None
            return float(x)
        except Exception:
            return None

    store = _StoreShim()

    out = []
    for t in list(_MANAGED_TRADES.values()):
        if not isinstance(t, dict):
            continue

        if account_id and str(t.get("account_id") or "") != str(account_id):
            continue
        if trade_id and str(t.get("trade_id") or "") != str(trade_id):
            continue
        if symbol and str(t.get("symbol") or "").upper() != str(symbol).upper():
            continue

        st = str(t.get("state") or "").upper()
        if st in ("DONE", "ERROR"):
            continue

        sym = str(t.get("symbol") or "").upper()
        md = store.get_md(sym) or {}

        lt = _as_float(md.get("lt") or md.get("last") or md.get("price"))
        bid = _as_float(md.get("bid"))
        ask = _as_float(md.get("ask"))
        last_px = lt if lt is not None else (bid if bid is not None else ask)

        side = str(t.get("side") or "buy").lower()
        kind = "short" if side in ("sell", "short") else "long"

        stop_px = _as_float(t.get("soft_stop_price") or t.get("stop_price"))
        target_px = _as_float(t.get("soft_target_price") or t.get("take_profit_price") or t.get("target_price"))
        entry_px = _as_float(t.get("entry_avg_price"))

        hit_stop = False
        hit_target = False
        if last_px is not None and stop_px is not None and target_px is not None:
            if kind == "long":
                hit_stop = (stop_px > 0) and (last_px <= stop_px)
                hit_target = (target_px > 0) and (last_px >= target_px)
            else:
                hit_stop = (stop_px > 0) and (last_px >= stop_px)
                hit_target = (target_px > 0) and (last_px <= target_px)

        out.append(
            {
                "trade_id": t.get("trade_id"),
                "account_id": t.get("account_id"),
                "symbol": sym,
                "state": st,
                "kind": kind,
                "entry_avg_price": entry_px,
                "stop_px": stop_px,
                "target_px": target_px,
                "md": {"lt": lt, "bid": bid, "ask": ask, "updated_ts": md.get("updated_ts")},
                "last_px_used_by_trader": last_px,
                "hit_stop": hit_stop,
                "hit_target": hit_target,
            }
        )

    # Most relevant first: trades where we *think* it should hit
    out.sort(key=lambda x: (x["hit_stop"] or x["hit_target"], x.get("symbol") or ""), reverse=True)
    return {"items": out}

@app.get("/v1/health")
async def health():
    return {"ok": True, "ts": time.time(), "iso_utc": _iso_now()}

@app.get("/v1/time")
async def time_now():
    return {"ts": time.time(), "iso_utc": _iso_now()}


@app.get("/v1/events")
async def events(limit: int = Query(200, ge=1, le=1000)):
    """
    BrokerView compatibility endpoint.

    Returns recent trader-side events (cache). If no event plumbing is enabled yet,
    this safely returns an empty list.
    """
    try:
        # PortfolioManager keeps a small ring-buffer per account; expose aggregated view.
        items = []
        try:
            pm_events = getattr(pm, "_events", {}) or {}
            for aid, dq in pm_events.items():
                for ev in (dq or []):
                    if isinstance(ev, dict):
                        items.append({"account_id": aid, **ev})
        except Exception:
            pass

        # Intent history (if present)
        try:
            for ev in list(_INTENT_HISTORY)[-limit:]:
                if isinstance(ev, dict):
                    items.append(ev)
        except Exception:
            pass

        # newest-first
        items = items[-limit:]
        # Enrich for BrokerView: include broker_id/label fields (additive; backward compatible)
        enriched: list[dict] = []
        for it in items:
            try:
                if isinstance(it, dict):
                    aid = it.get("account_id") or ""
                    broker_id = (aid.split(":", 1)[0] if ":" in aid else "")
                    d = dict(it)
                    if broker_id:
                        d.setdefault("broker_id", broker_id)
                    d.setdefault("label", aid)
                    enriched.append(d)
                else:
                    enriched.append(it)
            except Exception:
                enriched.append(it)

        return {"items": enriched}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))




@app.get("/v1/accounts")
async def v1_accounts():
    accounts = await _get_accounts_somehow()
    # accounts must be a python list of dicts / pydantic models
    return JSONResponse({"accounts": accounts})


@app.get("/v1/portfolio/overview")
async def portfolio_overview():
    """
    BrokerView expects this endpoint.
    Keep it cache-only: use PortfolioManager state/snapshots.
    """
    try:
        return pm.get_state()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/v1/portfolio/positions")
async def portfolio_positions(account_id: str = Query(...)):
    """
    BrokerView expects this endpoint.

    Cache-only: return the current cached snapshot positions for that account.
    (No broker calls here; PortfolioManager refresh loop owns reconciliation.)
    """
    try:
        snaps = pm.get_snapshots()
        snap = snaps.get(account_id) or {}
        positions = snap.get("positions") or []
        return {"items": positions, "account_id": account_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
@app.get("/v1/orders")
async def get_orders(
    account_id: Optional[str] = Query(None),
    status: Optional[str] = Query("active"),
    limit: int = Query(200, ge=1, le=2000),
):
    try:
        docs = []
        for o in list(_LOCAL_ORDERS.values()):
            if account_id and o.get("account_id") != account_id:
                continue
            st = str(o.get("status") or "")
            if (status or "active") == "active":
                if not _is_active_status(st):
                    continue
            elif (status or "") == "closed":
                if not _is_closed_status(st):
                    continue
            docs.append(_local_order_doc(o))
        docs.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
        return {"items": docs[: int(limit)], "stale": False}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))




@app.post("/v1/orders")
@app.post("/v1/orders/place")
async def api_orders_place(request: Request):
    """
    Place an order via the account adapter.

    Required by BrokerView. Performs a local echo into _LOCAL_ORDERS and then
    calls the adapter's place_order (or submit/create).
    """
    try:
        data = await request.json()
    except Exception:
        data = {}

    aid = (data.get("account_id") or "").strip() or None
    if not aid:
        aid = _default_account_id()
    if not aid:
        return JSONResponse({"ok": False, "error": "account_id required (multiple accounts present)"}, status_code=400)

    if aid not in pm.adapters:
        return JSONResponse({"ok": False, "error": "unknown account_id"}, status_code=404)

    args = {
        "symbol": data.get("symbol"),
        "side": data.get("side"),
        "qty": data.get("qty"),
        "type": data.get("type", "market"),
        "time_in_force": data.get("time_in_force", data.get("tif", "day")),
        "limit_price": data.get("limit_price"),
        "stop_price": data.get("stop_price"),
        "trail": data.get("trail"),
        "extended_hours": bool(data.get("extended_hours", False)),
        "note": data.get("note") or "manual",
        "client_order_id": data.get("client_order_id"),
    }

    client_id = str(uuid4())
    submitted_at = _iso_now()

    note = str(args.get("note") or "")
    if "cid=" not in note:
        note = (note + "; " if note else "") + f"cid={client_id}"
    args["note"] = note

    local = {
        "account_id": aid,
        "id": client_id,
        "symbol": args.get("symbol"),
        "side": args.get("side"),
        "qty": args.get("qty"),
        "type": args.get("type"),
        "time_in_force": args.get("time_in_force"),
        "limit_price": args.get("limit_price"),
        "stop_price": args.get("stop_price"),
        "status": "pending_local",
        "submitted_at": submitted_at,
        "updated_at": submitted_at,
        "source": "manual",
        "note": note,
    }
    _LOCAL_ORDERS[client_id] = local

    if not args["symbol"] or not args["side"] or not args["qty"]:
        _fail_local(client_id, aid, "symbol, side, qty required")
        return JSONResponse({"ok": False, "error": "symbol, side, qty required"}, status_code=400)

    ad = pm.adapters[aid]
    fn = getattr(ad, "place_order", None)
    if fn is None:
        for alt in ("submit_order", "create_order", "submit"):
            alt_fn = getattr(ad, alt, None)
            if alt_fn is not None:
                fn = alt_fn
                break

    if fn is None:
        _fail_local(client_id, aid, "adapter does not support place_order")
        return JSONResponse({"ok": False, "error": "adapter does not support place_order"}, status_code=400)

    try:
        res = await _call_maybe_async(fn, **args)
        bid = _broker_id_from_result(res)
        if bid:
            _CLIENT_TO_BROKER[client_id] = str(bid)
            o = _LOCAL_ORDERS.get(client_id)
            if o:
                o["status"] = "submitted"
                o["broker_order_id"] = str(bid)
                o["updated_at"] = _iso_now()
        return {"ok": True, "account_id": aid, "client_order_id": client_id, "order": res}
    except Exception as e:
        _fail_local(client_id, aid, str(e))
        return JSONResponse({"ok": False, "error": str(e), "client_order_id": client_id}, status_code=400)


@app.post("/v1/orders/cancel_all")
async def api_orders_cancel_all(request: Request):
    """
    Best-effort cancel all open orders for an account.
    Requires adapter support (cancel_all_orders/cancel_all).
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    aid = (data.get("account_id") or "").strip() or None
    if not aid:
        aid = _default_account_id()
    if not aid:
        return JSONResponse({"ok": False, "error": "account_id required (multiple accounts present)"}, status_code=400)
    if aid not in pm.adapters:
        return JSONResponse({"ok": False, "error": "unknown account_id"}, status_code=404)

    ad = pm.adapters[aid]
    fn = getattr(ad, "cancel_all_orders", None) or getattr(ad, "cancel_all", None)
    if fn is None:
        return JSONResponse({"ok": False, "error": "adapter does not support cancel_all"}, status_code=400)

    try:
        res = await _call_maybe_async(fn)
        return {"ok": True, "account_id": aid, "result": res}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/v1/accounts/cancel_all")
async def api_accounts_cancel_all(request: Request):
    return await api_orders_cancel_all(request)


@app.get("/v1/portfolio")
async def portfolio_state():
    # Keep your newer endpoint too.
    try:
        return pm.get_state()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
