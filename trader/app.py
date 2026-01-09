# trader/app.py
# v2.1.0 – Trader HTTP API for BrokerView and auto-intent bridge.
#
# HARD RULES (enforced):
#   1) UI-poll endpoints MUST be cache-only:
#        - /v1/portfolio/positions reads pm.portfolio cache only
#        - /v1/orders reads local ledger cache only
#      They NEVER call the broker.
#   2) Broker I/O happens only in background tasks:
#        - startup sync
#        - websocket trade updates (Alpaca)
#        - periodic reconcile + periodic orders cache refresh
#   3) If broker is slow/offline, endpoints still return cached data with stale flags.
#
# This prevents BrokerView timeouts and “Failed to fetch” during demos.

import os
import time
import random
import asyncio
import logging
import threading
import json as _json
from uuid import uuid4
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Deque
from collections import deque as _deque

from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse as _StreamingResponse

from .portfolio_manager import PortfolioManager
from .alpaca_trade_updates import listen_trade_updates
from .trade_runner import TradeRunner

log = logging.getLogger("trader.app")
app = FastAPI(title="Reflex Trader API", version="2.1.0")

# ---------------------------------------------------------------------------
# Global portfolio manager instance
# ---------------------------------------------------------------------------

pm = PortfolioManager()

# ---------------------------------------------------------------------------
# Async safety – never block the event loop
# ---------------------------------------------------------------------------

async def _call_maybe_async(fn, *args, **kwargs):
    """
    Call fn safely from the asyncio loop.
    - If fn is async -> await it
    - If fn is sync  -> run it in a worker thread
    """
    if fn is None:
        return None
    if asyncio.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    return await asyncio.to_thread(fn, *args, **kwargs)

# ---------------------------------------------------------------------------
# Local Orders Ledger (cache)
# ---------------------------------------------------------------------------

_LOCAL_ORDERS: Dict[str, Dict[str, Any]] = {}     # key: broker id when known, else client id
_CLIENT_TO_BROKER: Dict[str, str] = {}            # client_id -> broker_id

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()

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
        "broker_order_id": o.get("broker_order_id") or o.get("id"),
        "filled_qty": o.get("filled_qty"),
        "filled_avg_price": o.get("filled_avg_price"),
        "source": o.get("source"),
        "note": o.get("note"),
        "error": o.get("error"),
    }

def _fail_local(client_id: str, aid: str, reason: str, status: str = "failed") -> None:
    try:
        o = _LOCAL_ORDERS.get(client_id)
        if not o:
            return
        o["status"] = status
        o["error"] = reason
        o["updated_at"] = _iso_now()
        publish_event({
            "type": "ORDER_LOCAL_FAILED",
            "ts": time.time(),
            "account_id": aid,
            "order": _local_order_doc(o),
            "error": reason,
        })
    except Exception:
        pass

def _upsert_local_from_broker(aid: str, broker_doc: Dict[str, Any]) -> None:
    """
    Merge broker order snapshot into local ledger.
    Uses broker id as the stable key.
    """
    bid = broker_doc.get("id")
    if not bid:
        return

    client_id = None
    for cid, mapped in list(_CLIENT_TO_BROKER.items()):
        if mapped == bid:
            client_id = cid
            break

    existing = _LOCAL_ORDERS.get(bid)
    if not existing and client_id:
        existing = _LOCAL_ORDERS.get(client_id)

    if existing:
        merged = dict(existing)
        merged.update({k: v for k, v in broker_doc.items() if v is not None})
        merged["account_id"] = aid
        merged["id"] = bid
        merged["updated_at"] = _iso_now()
        _LOCAL_ORDERS[bid] = merged
        if client_id and client_id in _LOCAL_ORDERS:
            _LOCAL_ORDERS.pop(client_id, None)
    else:
        merged = dict(broker_doc)
        merged["account_id"] = aid
        merged["id"] = bid
        merged["updated_at"] = _iso_now()
        _LOCAL_ORDERS[bid] = merged

# ---------------------------------------------------------------------------
# Trader Event Hub (SSE)
# ---------------------------------------------------------------------------

_EVENT_HISTORY = _deque(maxlen=500)
_EVENT_SUBSCRIBERS = set()          # set[asyncio.Queue]
_MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None

def _event_payload(evt: Dict[str, Any]) -> Dict[str, Any]:
    if "ts" not in evt:
        evt = dict(evt)
        evt["ts"] = time.time()
    if "id" not in evt:
        evt = dict(evt)
        evt["id"] = str(uuid4())
    return evt

def publish_event(evt: Dict[str, Any]) -> None:
    payload = _event_payload(evt)

    def _do_publish() -> None:
        _EVENT_HISTORY.append(payload)
        dead = []
        for q in list(_EVENT_SUBSCRIBERS):
            try:
                q.put_nowait(payload)
            except Exception:
                dead.append(q)
        for q in dead:
            _EVENT_SUBSCRIBERS.discard(q)

    try:
        loop = _MAIN_LOOP
        if loop and loop.is_running():
            loop.call_soon_threadsafe(_do_publish)
        else:
            _do_publish()
    except Exception:
        pass

async def _sse_event_generator(request: Request):
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _EVENT_SUBSCRIBERS.add(q)

    # replay last events
    try:
        for evt in list(_EVENT_HISTORY)[-50:]:
            yield f"data: {_json.dumps(evt, separators=(',',':'))}\n\n"
    except Exception:
        pass

    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                evt = await asyncio.wait_for(q.get(), timeout=15.0)
                yield f"data: {_json.dumps(evt, separators=(',',':'))}\n\n"
            except asyncio.TimeoutError:
                yield ": ping\n\n"
            except Exception:
                yield ": error\n\n"
    finally:
        _EVENT_SUBSCRIBERS.discard(q)

# ---------------------------------------------------------------------------
# Pending-local expiry
# ---------------------------------------------------------------------------

_PENDING_LOCAL_TTL_SECS: float = float(os.getenv("TRADER_PENDING_LOCAL_TTL_SECS", "8") or 8)

def _parse_iso_ts(s: Any) -> Optional[float]:
    try:
        if not s:
            return None
        if isinstance(s, (int, float)):
            return float(s)
        if isinstance(s, datetime):
            dt = s
        else:
            ss = str(s).strip()
            if ss.endswith("Z") and "+" not in ss:
                ss = ss[:-1] + "+00:00"
            dt = datetime.fromisoformat(ss)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return float(dt.timestamp())
    except Exception:
        return None

def _pending_age_secs(o: Dict[str, Any], now_ts: Optional[float] = None) -> Optional[float]:
    try:
        now = float(now_ts if now_ts is not None else time.time())
        ts = _parse_iso_ts(o.get("submitted_at")) or _parse_iso_ts(o.get("created_at"))
        if ts is None:
            return None
        return max(0.0, now - ts)
    except Exception:
        return None

def _expire_stale_pending_locals(now_ts: Optional[float] = None) -> int:
    ttl = max(2.0, min(float(_PENDING_LOCAL_TTL_SECS), 120.0))
    now = float(now_ts if now_ts is not None else time.time())
    expired = 0
    for oid, o in list(_LOCAL_ORDERS.items()):
        try:
            if str(o.get("status") or "").lower() != "pending_local":
                continue
            age = _pending_age_secs(o, now_ts=now)
            if age is None or age < ttl:
                continue
            o["status"] = "failed"
            o["error"] = f"pending_local_timeout>{ttl:.0f}s"
            o["updated_at"] = _iso_now()
            expired += 1
            publish_event({"type": "ORDER_LOCAL_EXPIRED", "ts": now, "account_id": o.get("account_id"), "order": _local_order_doc(o), "age_s": age, "ttl_s": ttl})
        except Exception:
            continue
    return expired

def _broker_id_from_result(res: Any) -> Optional[str]:
    try:
        if res is None:
            return None
        if hasattr(res, "to_dict"):
            d = res.to_dict()
        elif isinstance(res, dict):
            d = res
        else:
            return None
        bid = d.get("id") or d.get("order_id") or d.get("broker_order_id")
        return str(bid) if bid else None
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Managed trades / journal / runner (kept as in prior versions)
# ---------------------------------------------------------------------------

_MANAGED_TRADES: Dict[str, Dict[str, Any]] = {}
_INTENT_HISTORY: Deque[Dict[str, Any]] = _deque(maxlen=500)

_JOURNAL_MTIME: float = 0.0
_JOURNAL_LOCK = threading.Lock()

_bg_reconcile_task: Optional[asyncio.Task] = None
_bg_events_tasks: List[asyncio.Task] = []
_bg_events_stop: Optional[asyncio.Event] = None
_bg_orders_task: Optional[asyncio.Task] = None

_TRADE_RUNNER: Optional[TradeRunner] = None

def _truthy_env(name: str, default: str = "0") -> bool:
    v = str(os.getenv(name, default)).strip().lower()
    return v in ("1", "true", "yes", "y", "on")

def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

_JOURNAL_ENABLED: bool = _truthy_env("REFLEX__TRADER_JOURNAL", "0")

def _journal_path() -> str:
    p = (os.getenv("REFLEX__TRADER_JOURNAL_PATH") or "").strip()
    if p:
        return p
    return os.path.join(_repo_root(), "logs", "trader", "trade_journal.jsonl")

def _journal_append(kind: str, payload: Dict[str, Any]) -> None:
    if not _JOURNAL_ENABLED:
        return
    try:
        jp = _journal_path()
        os.makedirs(os.path.dirname(jp), exist_ok=True)
        rec = {"ts": time.time(), "kind": kind, "payload": payload}
        with open(jp, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, separators=(",", ":"), ensure_ascii=False))
            f.write("\n")
    except Exception:
        log.exception("journal.append.failed kind=%r", kind)

def _journal_replay() -> None:
    if not _JOURNAL_ENABLED:
        return
    jp = _journal_path()
    if not os.path.exists(jp):
        return
    try:
        intents: dict[str, dict] = {}
        trades: dict[str, dict] = {}
        with open(jp, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _json.loads(line)
                except Exception:
                    continue
                kind = rec.get("kind")
                payload = rec.get("payload") or {}
                if kind == "intent":
                    iid = payload.get("intent_id") or payload.get("id")
                    if iid:
                        intents[str(iid)] = payload
                elif kind == "trade":
                    tid = payload.get("trade_id") or payload.get("id")
                    if tid:
                        trades[str(tid)] = payload
        for it in intents.values():
            _INTENT_HISTORY.append(it)
        for tid, tr in trades.items():
            _MANAGED_TRADES[tid] = tr
    except Exception:
        log.exception("journal.replay.failed path=%r", jp)

def _journal_sync_if_needed() -> None:
    global _JOURNAL_MTIME
    if not _JOURNAL_ENABLED:
        return
    jp = _journal_path()
    try:
        st = os.stat(jp)
    except Exception:
        return
    mtime = float(getattr(st, "st_mtime", 0.0) or 0.0)
    if mtime <= _JOURNAL_MTIME:
        return
    with _JOURNAL_LOCK:
        try:
            st2 = os.stat(jp)
            mtime2 = float(getattr(st2, "st_mtime", 0.0) or 0.0)
        except Exception:
            return
        if mtime2 <= _JOURNAL_MTIME:
            return
        _journal_replay()
        _JOURNAL_MTIME = mtime2

_journal_replay()

def _new_trade_id() -> str:
    return str(uuid4())

def _new_intent_id() -> str:
    return str(uuid4())

def _default_account_id() -> Optional[str]:
    try:
        if len(pm.adapters) == 1:
            return next(iter(pm.adapters.keys()))
    except Exception:
        pass
    return None

# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------

def _coerce_positions(raw_positions: List[Any], account_id: str) -> List[Dict[str, Any]]:
    norm: List[Dict[str, Any]] = []
    for p in raw_positions or []:
        if hasattr(p, "to_dict"):
            d = p.to_dict()
        elif isinstance(p, dict):
            d = dict(p)
        else:
            d = {"raw": repr(p)}

        sym = d.get("symbol") or d.get("asset_id") or d.get("asset") or d.get("ticker")
        qty = d.get("qty", d.get("quantity", 0) or 0) or 0
        avg_price = d.get("avg_price", d.get("avg_fill_price", d.get("cost_basis", 0) or 0))
        mkt_price = d.get("market_price", d.get("current_price", d.get("last_price", 0) or 0))
        side = d.get("side") or ("long" if float(qty) >= 0 else "short")

        norm.append({
            "account_id": account_id,
            "symbol": sym,
            "qty": float(qty),
            "avg_price": float(avg_price or 0),
            "market_price": float(mkt_price or 0),
            "side": side,
        })
    return norm

def _coerce_order_dict(o: Any, account_id: str) -> Dict[str, Any]:
    if hasattr(o, "to_dict"):
        d = o.to_dict()
    elif isinstance(o, dict):
        d = dict(o)
    else:
        d = {"raw": repr(o)}

    order_id = d.get("id") or d.get("order_id") or d.get("client_order_id")
    sym = d.get("symbol") or d.get("asset") or d.get("ticker")
    qty = d.get("qty", d.get("quantity", 0) or 0) or 0
    status = d.get("status") or d.get("state")
    submitted_at = d.get("submitted_at") or d.get("created_at") or d.get("timestamp")

    return {
        "account_id": account_id,
        "id": order_id,
        "symbol": sym,
        "side": d.get("side"),
        "qty": float(qty),
        "type": d.get("type"),
        "limit_price": d.get("limit_price"),
        "stop_price": d.get("stop_price"),
        "time_in_force": d.get("time_in_force") or d.get("tif"),
        "status": status,
        "submitted_at": submitted_at,
        "updated_at": d.get("updated_at") or d.get("updatedAt"),
        "client_order_id": d.get("client_order_id"),
        "broker_order_id": d.get("broker_order_id") or d.get("id"),
        "filled_qty": d.get("filled_qty"),
        "filled_avg_price": d.get("filled_avg_price"),
    }

def _snapshot_for_account(aid: str) -> Dict[str, Any]:
    s = pm.portfolio.get(aid)
    if s is None:
        return {"account_id": aid, "cash": 0.0, "equity": 0.0, "buying_power": 0.0, "positions": [], "updated_at": None}
    raw_positions = getattr(s, "positions", [])
    positions = _coerce_positions(raw_positions, aid)
    return {
        "account_id": aid,
        "cash": float(getattr(s, "cash", 0.0)),
        "equity": float(getattr(s, "equity", 0.0)),
        "buying_power": float(getattr(s, "buying_power", 0.0)),
        "positions": positions,
        "updated_at": getattr(s, "updated_at", None),
    }

# ---------------------------------------------------------------------------
# Broker order listing – USED ONLY by background refresh (not endpoints)
# ---------------------------------------------------------------------------

async def _broker_list_orders(aid: str, status: str) -> List[Dict[str, Any]]:
    ad = pm.adapters.get(aid)
    if ad is None:
        return []

    if status == "active":
        attr_names = ("list_open_orders", "list_active_orders", "open_orders")
    else:
        attr_names = ("list_closed_orders", "list_orders", "closed_orders")

    func = None
    for name in attr_names:
        func = getattr(ad, name, None)
        if func is not None:
            break
    if func is None:
        return []

    try:
        # Hard time bound; do not let broker stall the refresh loop
        timeout = max(0.5, min(float(os.getenv("TRADER_BROKER_ORDERS_TIMEOUT_SECS", "2.5") or 2.5), 15.0))
        coro = _call_maybe_async(func)
        orders = await asyncio.wait_for(coro, timeout=timeout)
        return [_coerce_order_dict(o, aid) for o in (orders or [])]
    except asyncio.TimeoutError:
        publish_event({"type": "BROKER_ORDERS_TIMEOUT", "ts": time.time(), "account_id": aid, "status": status})
        return []
    except Exception:
        log.exception("broker.list_orders.failed", extra={"account_id": aid, "status": status})
        return []

# ---------------------------------------------------------------------------
# TradeRunner
# ---------------------------------------------------------------------------

def _ensure_trade_runner() -> TradeRunner:
    global _TRADE_RUNNER
    if _TRADE_RUNNER is None:
        _TRADE_RUNNER = TradeRunner(
            pm=pm,
            trades=_MANAGED_TRADES,
            journal_append=_journal_append,
            publish_event=publish_event,
            list_orders_for_account_async=_broker_list_orders,  # ok: trade runner isn't UI-hot
            snapshot_for_account=_snapshot_for_account,
        )
    return _TRADE_RUNNER

# ---------------------------------------------------------------------------
# Background: reconcile + orders cache refresh
# ---------------------------------------------------------------------------

POLL_SECS = float(os.getenv("TRADER_RECONCILE_INTERVAL", "10"))
_ORDERS_REFRESH_SECS = float(os.getenv("TRADER_ORDERS_REFRESH_INTERVAL", "5"))

_LAST_POLL_TS: float = 0.0
_LAST_POLL_ERR: Optional[str] = None

_LAST_ORDERS_SYNC_TS: float = 0.0
_LAST_ORDERS_SYNC_ERR: Optional[str] = None

def _last_poll_iso() -> Optional[str]:
    if not _LAST_POLL_TS:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(_LAST_POLL_TS))

def _last_orders_sync_iso() -> Optional[str]:
    if not _LAST_ORDERS_SYNC_TS:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(_LAST_ORDERS_SYNC_TS))

async def _background_reconciler():
    global _LAST_POLL_TS, _LAST_POLL_ERR
    while True:
        base = max(3.0, min(POLL_SECS, 30.0))
        jitter = random.uniform(-base * 0.1, base * 0.1)
        sleep_for = max(3.0, min(base + jitter, 30.0))
        try:
            await asyncio.sleep(sleep_for)
        except asyncio.CancelledError:
            break

        try:
            fn = getattr(pm, "reconcile_once", None)
            if fn is not None:
                await _call_maybe_async(fn)
            _LAST_POLL_TS = time.time()
            _LAST_POLL_ERR = None
        except Exception as e:
            _LAST_POLL_TS = time.time()
            _LAST_POLL_ERR = str(e)
            log.exception("background_reconcile.failed")

async def _refresh_orders_cache_once():
    """
    Pull broker orders in the background and merge into local ledger.
    Never called from request handlers.
    """
    global _LAST_ORDERS_SYNC_TS, _LAST_ORDERS_SYNC_ERR
    try:
        # expire local ghosts before merge
        _expire_stale_pending_locals()

        for aid in list(pm.adapters.keys()):
            # active orders
            active = await _broker_list_orders(aid, "active")
            for od in active:
                _upsert_local_from_broker(aid, od)

            # closed orders (best effort; may be empty depending on adapter)
            closed = await _broker_list_orders(aid, "closed")
            for od in closed:
                _upsert_local_from_broker(aid, od)

        _LAST_ORDERS_SYNC_TS = time.time()
        _LAST_ORDERS_SYNC_ERR = None
    except Exception as e:
        _LAST_ORDERS_SYNC_TS = time.time()
        _LAST_ORDERS_SYNC_ERR = str(e)
        log.exception("orders_cache_refresh.failed")

async def _background_orders_cache_refresh():
    base = max(2.0, min(_ORDERS_REFRESH_SECS, 30.0))
    while True:
        try:
            jitter = random.uniform(-base * 0.15, base * 0.15)
            await asyncio.sleep(max(1.0, base + jitter))
        except asyncio.CancelledError:
            break
        await _refresh_orders_cache_once()

# ---------------------------------------------------------------------------
# Phase 2 (minimal): Risk + Plan compilation (unchanged stubs)
# ---------------------------------------------------------------------------

def _risk_decision_v1(account_id: str, symbol: str, side: str, raw_intent: Dict[str, Any]) -> Dict[str, Any]:
    src = str(raw_intent.get("source") or "bot").lower()
    requested_shares = raw_intent.get("shares") or raw_intent.get("qty")
    shares = 10
    try:
        if src == "manual" and requested_shares is not None:
            shares = max(1, min(int(requested_shares), 1000))
    except Exception:
        shares = 10

    max_total_risk_dollars = 50.0
    return {
        "ok": True,
        "account_id": account_id,
        "symbol": symbol,
        "side": side,
        "shares": shares,
        "max_total_risk_$": max_total_risk_dollars,
        "allow_addons": False,
        "max_addons": 0,
        "notes": "phase2 risk stub",
    }

def _compile_plan_v1(intent_doc: Dict[str, Any], risk: Dict[str, Any]) -> Dict[str, Any]:
    trigger = intent_doc.get("trigger") or {"kind": "immediate"}
    if isinstance(trigger, dict) and "type" in trigger and "kind" not in trigger:
        trigger = {**trigger, "kind": trigger.get("type")}

    entry_plan = {"kind": "entry", "style": "market", "trigger": trigger, "shares": risk.get("shares")}
    exit_plan = {"kind": "exit", "hard_stop": {"mode": "computed_by_trade_runner"}, "soft_stop": {"mode": "disabled"}}
    scale_plan = {"mode": "disabled", "tranches": [], "notes": "phase2"}
    return {"version": 1, "entry": entry_plan, "exit": exit_plan, "scale": scale_plan}

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {"ok": True, "service": "Reflex Trader API", "adapters": list(pm.adapters.keys()), "portfolio_accounts": list(pm.portfolio.keys())}

@app.get("/v1/time")
async def api_time():
    now = datetime.now(timezone.utc)
    return {
        "ok": True,
        "server_utc_ms": int(now.timestamp() * 1000),
        "server_iso": now.isoformat().replace("+00:00", "Z"),
        "source": "trader",
        "stale": False,
    }

@app.get("/v1/events")
async def stream_events(request: Request):
    headers = {"Cache-Control": "no-cache", "Connection": "keep-alive"}
    return _StreamingResponse(_sse_event_generator(request), media_type="text/event-stream", headers=headers)

@app.get("/v1/portfolio/accounts")
async def api_list_accounts():
    return {"ok": True, "accounts": list(pm.adapters.keys())}

@app.get("/v1/portfolio/snapshot")
async def api_snapshot(account_id: Optional[str] = Query(None, alias="account_id")):
    if account_id:
        return {"ok": True, "snapshot": _snapshot_for_account(account_id)}
    snaps = {aid: _snapshot_for_account(aid) for aid in pm.adapters.keys()}
    return {"ok": True, "snapshots": snaps}

@app.get("/v1/portfolio/overview")
async def api_overview():
    overview: Dict[str, Any] = {}
    for aid, adapter in pm.adapters.items():
        snap = _snapshot_for_account(aid)
        balances = {"cash": snap["cash"], "equity": snap["equity"], "buying_power": snap["buying_power"], "updated_at": snap["updated_at"]}
        positions = snap["positions"]
        broker_id = getattr(adapter, "broker_id", None) or getattr(adapter, "broker", None)
        kind = getattr(adapter, "kind", None) or getattr(adapter, "adapter_kind", None) or getattr(adapter, "name", None) or "unknown"
        overview[aid] = {"balances": balances, "positions": positions, "broker_id": broker_id, "kind": kind}
    return {"overview": overview, "count": len(overview), "live_only": False, "account_id": None}

@app.get("/v1/portfolio/positions")
async def api_positions(account_id: Optional[str] = Query(None, alias="account_id")):
    # Cache-only: never broker I/O
    positions: List[Dict[str, Any]] = []
    if account_id:
        positions.extend(_snapshot_for_account(account_id)["positions"])
    else:
        for aid in pm.adapters.keys():
            positions.extend(_snapshot_for_account(aid)["positions"])
    return {
        "ok": True,
        "positions": positions,
        "stale": bool(_LAST_POLL_ERR),
        "last_sync_at": _last_poll_iso(),
        "last_sync_error": _LAST_POLL_ERR,
    }

@app.post("/v1/portfolio/reconcile")
async def api_reconcile(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    aid = data.get("account_id")

    try:
        fn = getattr(pm, "reconcile_once", None)
        if fn is None:
            raise HTTPException(status_code=500, detail="PortfolioManager has no reconcile_once")
        await _call_maybe_async(fn, account_id=aid)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("reconcile_once.failed", extra={"account_id": aid})
        raise HTTPException(status_code=500, detail=str(e)) from e

    # refresh orders cache after reconcile (best effort)
    try:
        await _refresh_orders_cache_once()
    except Exception:
        pass

    if aid:
        snapshot = _snapshot_for_account(aid)
        return {"ok": True, "message": "reconciled", "accounts": [aid], "results": {aid: {"status": "ok", "snapshot": snapshot}}}

    results: Dict[str, Any] = {k: {"status": "ok", "snapshot": _snapshot_for_account(k)} for k in pm.adapters.keys()}
    return {"ok": True, "message": "reconciled", "accounts": list(pm.adapters.keys()), "results": results}

@app.post("/v1/portfolio/flatten")
async def api_flatten(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    aid = data.get("account_id")

    publish_event({"type": "FLATTEN_REQUEST", "account_id": aid, "ts": time.time()})
    try:
        fn = getattr(pm, "flatten", None)
        if fn is None:
            raise HTTPException(status_code=500, detail="PortfolioManager has no flatten")
        await _call_maybe_async(fn, account_id=aid)

        # reconcile after flatten
        fn2 = getattr(pm, "reconcile_account", None)
        if aid and fn2 is not None:
            await _call_maybe_async(fn2, aid)
        else:
            fn3 = getattr(pm, "reconcile_once", None)
            if fn3 is not None:
                await _call_maybe_async(fn3)

        publish_event({"type": "FLATTEN_DONE", "account_id": aid, "ts": time.time()})

        # refresh orders cache after flatten
        try:
            await _refresh_orders_cache_once()
        except Exception:
            pass

        return {"ok": True, "account_id": aid}
    except HTTPException:
        raise
    except Exception as e:
        log.exception("flatten.failed", extra={"account_id": aid})
        publish_event({"type": "FLATTEN_FAILED", "account_id": aid, "error": str(e), "ts": time.time()})
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

@app.get("/v1/orders")
async def api_list_orders(
    status: str = Query("active", alias="status"),
    account_id: Optional[str] = Query(None, alias="account_id"),
):
    """
    Cache-only orders listing.
    NO broker calls. Ever.
    """
    status = str(status or "active").lower().strip()
    if status not in ("active", "closed", "all"):
        status = "active"

    _expire_stale_pending_locals()

    out: List[Dict[str, Any]] = []
    for o in _LOCAL_ORDERS.values():
        if account_id and o.get("account_id") != account_id:
            continue
        st = o.get("status")
        if status == "active" and not _is_active_status(st):
            continue
        if status == "closed" and not _is_closed_status(st):
            continue
        out.append(_local_order_doc(o))

    out.sort(key=lambda x: str(x.get("submitted_at") or ""), reverse=True)

    stale = bool(_LAST_ORDERS_SYNC_ERR) or bool(_LAST_POLL_ERR)
    return {
        "ok": True,
        "orders": out,
        "stale": stale,
        "orders_last_sync_at": _last_orders_sync_iso(),
        "orders_last_sync_error": _LAST_ORDERS_SYNC_ERR,
    }

@app.post("/v1/orders")
@app.post("/v1/orders/place")
async def api_orders_place(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    aid = data.get("account_id")
    if not aid:
        if len(pm.adapters) == 1:
            aid = next(iter(pm.adapters.keys()))
        else:
            return JSONResponse({"ok": False, "error": "account_id required when multiple accounts present"}, status_code=400)

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
    }

    # Local echo
    client_id = str(uuid4())
    submitted_at = _iso_now()
    note = args.get("note") or ""
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
    publish_event({"type": "ORDER_LOCAL_NEW", "ts": time.time(), "account_id": aid, "order": _local_order_doc(local)})

    if not args["symbol"] or not args["side"] or not args["qty"]:
        _fail_local(client_id, aid, "symbol, side, qty required")
        return JSONResponse({"ok": False, "error": "symbol, side, qty required"}, status_code=400)

    broker_args = dict(args)

    pm_fn = getattr(pm, "place_order", None)
    if pm_fn is not None:
        try:
            res = await _call_maybe_async(pm_fn, account_id=aid, **broker_args)
            bid = _broker_id_from_result(res)
            if bid:
                _CLIENT_TO_BROKER[client_id] = bid
                # merge broker snapshot if convertible
                try:
                    od = _coerce_order_dict(res, aid) if (isinstance(res, dict) or hasattr(res, "to_dict")) else {"id": bid}
                    od["id"] = bid
                    _upsert_local_from_broker(aid, od)
                except Exception:
                    pass
            else:
                # at least mark as submitted
                o = _LOCAL_ORDERS.get(client_id)
                if o and str(o.get("status") or "").lower() == "pending_local":
                    o["status"] = "submitted"
                    o["updated_at"] = _iso_now()

            # best-effort background refresh soon
            try:
                asyncio.create_task(_refresh_orders_cache_once())
            except Exception:
                pass

            return {"ok": True, "account_id": aid, "order": res}
        except Exception as e:
            _fail_local(client_id, aid, str(e))
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    # Fallback: adapter place_order/submit_order/create_order (run safely)
    ad = pm.adapters[aid]
    fn = getattr(ad, "place_order", None)
    if fn is None:
        for alt in ("submit_order", "create_order"):
            alt_fn = getattr(ad, alt, None)
            if alt_fn is not None:
                fn = alt_fn
                break

    if fn is None:
        _fail_local(client_id, aid, "adapter does not support place_order")
        return JSONResponse({"ok": False, "error": "adapter does not support place_order"}, status_code=400)

    try:
        res = await _call_maybe_async(fn, **broker_args)
        bid = _broker_id_from_result(res)
        if bid:
            _CLIENT_TO_BROKER[client_id] = bid
        try:
            asyncio.create_task(_refresh_orders_cache_once())
        except Exception:
            pass
        return {"ok": True, "account_id": aid, "order": res}
    except Exception as e:
        _fail_local(client_id, aid, str(e))
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/v1/orders/cancel_all")
async def api_orders_cancel_all(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    aid = data.get("account_id")
    if not aid:
        if len(pm.adapters) == 1:
            aid = next(iter(pm.adapters.keys()))
        else:
            return JSONResponse({"ok": False, "error": "account_id required"}, status_code=400)

    if aid not in pm.adapters:
        return JSONResponse({"ok": False, "error": "unknown account_id"}, status_code=404)

    publish_event({"type": "CANCEL_ALL_REQUEST", "account_id": aid, "ts": time.time()})
    ad = pm.adapters[aid]

    fn = getattr(ad, "cancel_all_orders", None) or getattr(ad, "cancel_all", None)
    try:
        if fn is not None:
            await _call_maybe_async(fn)
        else:
            # best effort: use broker list (background-safe)
            open_orders = await _broker_list_orders(aid, status="active")
            cancel_one = getattr(ad, "cancel_order", None) or getattr(ad, "cancel", None)
            if cancel_one is None:
                return JSONResponse({"ok": False, "error": "adapter has no cancel method"}, status_code=400)
            for o in open_orders:
                oid = o.get("id") or o.get("order_id")
                if not oid:
                    continue
                await _call_maybe_async(cancel_one, oid)

        fn2 = getattr(pm, "reconcile_account", None)
        if fn2 is not None:
            await _call_maybe_async(fn2, aid)

        publish_event({"type": "CANCEL_ALL_DONE", "account_id": aid, "ts": time.time()})
        try:
            await _refresh_orders_cache_once()
        except Exception:
            pass
        return {"ok": True, "account_id": aid}
    except Exception as e:
        publish_event({"type": "CANCEL_ALL_FAILED", "account_id": aid, "error": str(e), "ts": time.time()})
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


# ---------------------------------------------------------------------------
# BrokerView endpoint aliases (contract compatibility)
# ---------------------------------------------------------------------------
# BrokerView calls these:
#   POST /v1/accounts/flatten
#   POST /v1/accounts/cancel_all
# In Trader v2.1.0 the canonical implementations are:
#   POST /v1/portfolio/flatten
#   POST /v1/orders/cancel_all
# These aliases keep UI buttons working without changing BrokerView.

@app.post("/v1/accounts/flatten")
async def api_accounts_flatten(request: Request):
    # Delegate to canonical endpoint implementation
    return await api_flatten(request)

@app.post("/v1/accounts/cancel_all")
async def api_accounts_cancel_all(request: Request):
    # Delegate to canonical endpoint implementation
    return await api_orders_cancel_all(request)


@app.get("/v1/intents")
async def api_list_intents(limit: int = Query(50, alias="limit")):
    _journal_sync_if_needed()
    try:
        n = max(1, min(int(limit or 50), 500))
    except Exception:
        n = 50
    items = list(_INTENT_HISTORY)[-n:]
    items.reverse()
    return {"ok": True, "intents": items}

@app.post("/v1/intents")
async def api_submit_intent(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    account_id = (data.get("account_id") or "").strip() or None
    if not account_id:
        account_id = _default_account_id()
    if not account_id:
        raise HTTPException(status_code=400, detail="account_id required (multiple accounts configured)")
    if account_id not in pm.adapters:
        raise HTTPException(status_code=404, detail="unknown account_id")

    symbol = str(data.get("symbol", "")).upper().strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol required")

    side = str(data.get("side", "buy")).lower().strip() or "buy"
    if side not in ("buy", "sell", "short", "cover"):
        raise HTTPException(status_code=400, detail="invalid side")

    source = str(data.get("source", "bot")).lower().strip() or "bot"
    if source not in ("bot", "manual"):
        source = "bot"

    strategy_id = str(data.get("strategy_id", "")).strip() or None
    trigger = data.get("trigger") or {"kind": "immediate"}

    intent_id = _new_intent_id()
    trade_id = _new_trade_id()
    now = time.time()

    intent_doc: Dict[str, Any] = {
        "intent_id": intent_id,
        "trade_id": trade_id,
        "created_ts": now,
        "account_id": account_id,
        "symbol": symbol,
        "side": side,
        "source": source,
        "strategy_id": strategy_id,
        "trigger": trigger,
        "raw": data,
    }
    _INTENT_HISTORY.append(intent_doc)
    _journal_append("intent", intent_doc)

    risk = _risk_decision_v1(account_id, symbol, side, data)
    plan = _compile_plan_v1(intent_doc, risk)

    trade_doc: Dict[str, Any] = {
        "trade_id": trade_id,
        "intent_id": intent_id,
        "created_ts": now,
        "account_id": account_id,
        "symbol": symbol,
        "side": side,
        "state": "READY",
        "strategy_id": strategy_id,
        "trigger": trigger,
        "source": source,
        "note": data.get("note") or "",
        "risk": risk,
        "plan": plan,
        "qty": float(risk.get("shares") or 0),
    }
    _MANAGED_TRADES[trade_id] = trade_doc
    _journal_append("trade", trade_doc)

    try:
        _ensure_trade_runner().enqueue(trade_id)
    except Exception:
        pass

    publish_event({"type": "INTENT_ACCEPTED", "ts": now, "account_id": account_id, "symbol": symbol, "trade_id": trade_id,
                   "intent_id": intent_id, "source": source, "strategy_id": strategy_id, "state": "READY"})
    publish_event({"type": "TRADE_READY", "ts": now, "account_id": account_id, "symbol": symbol, "trade_id": trade_id,
                   "intent_id": intent_id, "risk": {"shares": risk.get("shares"), "max_total_risk_$": risk.get("max_total_risk_$")},
                   "plan": {"entry": plan.get("entry"), "exit": plan.get("exit")}})

    return {"ok": True, "intent": intent_doc, "trade": trade_doc}

@app.get("/v1/trades")
async def api_list_trades(status: str = Query("planned", alias="status"), account_id: Optional[str] = Query(None, alias="account_id")):
    _journal_sync_if_needed()
    if status not in ("planned", "active", "closed"):
        status = "planned"

    planned_states = {"READY"}
    active_states = {"ENTRY_SUBMITTED", "ENTRY_FILLED", "PROTECT_SUBMITTED", "PROTECTED"}
    closed_states = {"DONE", "CANCELED", "ERROR", "REJECTED", "ABORT_PROTECTION"}

    out: List[Dict[str, Any]] = []
    for t in _MANAGED_TRADES.values():
        if account_id and t.get("account_id") != account_id:
            continue
        st = str(t.get("state") or "")
        if status == "planned" and st in planned_states:
            out.append(t)
        elif status == "active" and st in active_states:
            out.append(t)
        elif status == "closed" and st in closed_states:
            out.append(t)

    out.sort(key=lambda x: float(x.get("created_ts") or 0), reverse=True)
    return {"ok": True, "trades": out}

@app.get("/v1/trades/{trade_id}")
async def api_get_trade(trade_id: str):
    _journal_sync_if_needed()
    t = _MANAGED_TRADES.get(trade_id)
    if not t:
        raise HTTPException(status_code=404, detail="trade not found")
    return {"ok": True, "trade": t}

@app.get("/v1/health")
async def api_health():
    return {
        "ok": True,
        "adapters": list(pm.adapters.keys()),
        "portfolio_accounts": list(pm.portfolio.keys()),
        "last_snapshot_count": len(pm.portfolio or {}),
        "poll_seconds": POLL_SECS,
        "last_poll_at": _last_poll_iso(),
        "last_poll_error": _LAST_POLL_ERR,
        "orders_refresh_seconds": _ORDERS_REFRESH_SECS,
        "orders_last_sync_at": _last_orders_sync_iso(),
        "orders_last_sync_error": _LAST_ORDERS_SYNC_ERR,
    }

# ---------------------------------------------------------------------------
# Broker WS events (Alpaca) – best effort; never blocks the loop
# ---------------------------------------------------------------------------

async def _background_broker_events():
    global _bg_events_stop, _bg_events_tasks
    _bg_events_stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    tasks: List[asyncio.Task] = []
    for aid, ad in pm.adapters.items():
        try:
            if getattr(ad, "broker_id", "") != "alpaca":
                continue
            base = getattr(ad, "base", "") or ""
            key_id = getattr(ad, "key_id", "") or ""
            secret = getattr(ad, "secret", "") or ""
            if not key_id or not secret:
                continue

            def _on_evt_factory(account_id: str):
                def _on_evt(evt: Dict[str, Any]) -> None:
                    try:
                        publish_event({"type": "BROKER_UPDATE", "ts": time.time(), "account_id": account_id, "event": evt})
                    except Exception:
                        pass
                    try:
                        pm.record_broker_event(account_id, evt)
                    except Exception:
                        pass

                    # Trigger cache refresh without blocking caller thread
                    try:
                        asyncio.run_coroutine_threadsafe(_refresh_orders_cache_once(), loop)
                    except Exception:
                        pass

                    # Trigger reconcile without blocking caller thread
                    try:
                        fn = getattr(pm, "reconcile_account", None)
                        if fn is not None:
                            asyncio.run_coroutine_threadsafe(_call_maybe_async(fn, account_id), loop)
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

    _bg_events_tasks = tasks
    if tasks:
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass

# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def on_startup():
    global _MAIN_LOOP, _bg_reconcile_task, _bg_orders_task
    log.info("startup.begin")
    _MAIN_LOOP = asyncio.get_running_loop()

    # register adapters
    fn_reg = getattr(pm, "register_from_db", None)
    if fn_reg is None:
        raise RuntimeError("PortfolioManager missing register_from_db")
    await _call_maybe_async(fn_reg)

    # initial reconcile
    try:
        fn_rec = getattr(pm, "reconcile_once", None)
        if fn_rec is not None:
            await _call_maybe_async(fn_rec)
    except Exception:
        log.exception("startup.initial_reconcile.failed")

    # initial orders refresh (best effort)
    try:
        await _refresh_orders_cache_once()
    except Exception:
        pass

    # background loops
    loop = asyncio.get_event_loop()
    _bg_reconcile_task = loop.create_task(_background_reconciler())
    _bg_orders_task = loop.create_task(_background_orders_cache_refresh())
    loop.create_task(_background_broker_events())

    _ensure_trade_runner().start()
    log.info("startup.done")

@app.on_event("shutdown")
async def on_shutdown():
    global _bg_reconcile_task, _bg_orders_task, _bg_events_tasks, _bg_events_stop, _TRADE_RUNNER
    log.info("shutdown.begin")

    try:
        if _bg_events_stop is not None:
            _bg_events_stop.set()
    except Exception:
        pass

    for t in (_bg_reconcile_task, _bg_orders_task):
        if t:
            try:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
            except Exception:
                pass
    _bg_reconcile_task = None
    _bg_orders_task = None

    try:
        if _TRADE_RUNNER is not None:
            await _TRADE_RUNNER.stop()
    except Exception:
        pass

    _bg_events_tasks = []
    _bg_events_stop = None
    log.info("shutdown.done")
