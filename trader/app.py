# trader/app.py
# v2.0.0 – Trader HTTP API for BrokerView and auto-intent bridge.
#
# This module is the *canonical* contract between:
#   • Trader service
#   • Reflex BrokerView cockpit
#   • Evaluator → broker_worker auto-intent pipeline
#
# If you change any endpoint or response shape, update this header and
# BrokerView's models (cockpit/brokerview/models.py) together.
#
# ---------------------------------------------------------------------------
# ENDPOINT CONTRACTS (high-level)
# ---------------------------------------------------------------------------
#
# 1) GET /v1/portfolio/accounts
#    → { "ok": true, "accounts": ["alpaca:live", "alpaca:paper", "sim:cash", ...] }
#
# 2) GET /v1/portfolio/snapshot?account_id=alpaca:paper
#    → { "ok": true, "snapshot": Snapshot }
#
#    Snapshot:
#      {
#        "account_id": "alpaca:paper",
#        "cash": 10000.0,
#        "equity": 10000.0,
#        "buying_power": 20000.0,
#        "positions": [ PositionDoc, ... ],
#        "updated_at": "2025-11-19T13:42:00.123456"
#      }
#
# 3) GET /v1/portfolio/positions
#    → { "ok": true, "positions": [ PositionDoc, ... ] }
#
#    PositionDoc (minimum fields used by BrokerView):
#      {
#        "account_id": "alpaca:paper",
#        "symbol": "AAPL",
#        "qty": 1.0,
#        "avg_price": 170.50,
#        "market_price": 171.23,
#        "side": "long" | "short"
#      }
#
# 4) GET /v1/portfolio/overview
#    → {
#         "overview": {
#           "alpaca:paper": {
#             "balances": {
#               "cash": ...,
#               "equity": ...,
#               "buying_power": ...,
#               "updated_at": "..."
#             },
#             "positions": [ PositionDoc, ... ],
#             "open_orders": [ OrderDoc, ... ],
#             "broker_id": "alpaca",
#             "kind": "alpaca"
#           },
#           ...
#         },
#         "count": 4,
#         "live_only": false,
#         "account_id": null
#       }
#
# 5) GET /v1/orders?status=active|closed[&account_id=...]
#    → { "ok": true, "orders": [ OrderDoc, ... ] }
#
#    OrderDoc (minimum fields used by BrokerView):
#      {
#        "account_id": "alpaca:paper",
#        "id": "broker-order-id",
#        "symbol": "AAPL",
#        "side": "buy" | "sell",
#        "qty": 1.0,
#        "type": "market" | "limit" | ...,
#        "limit_price": 170.0 | null,
#        "stop_price": 169.5 | null,
#        "status": "new" | "partially_filled" | "filled" | "canceled" | ...,
#        "submitted_at": "2025-11-19T13:42:00.123456"
#      }
#
# 6) POST /v1/orders         (auto-intent bridge)
#    POST /v1/orders/place   (BrokerView UI)
#
#    Request JSON (both paths identical):
#      {
#        "account_id": "alpaca:paper",    # optional if only one account exists
#        "symbol": "AAPL",
#        "side": "buy" | "sell",
#        "qty": 1,
#        "type": "market" | "limit" | "stop" | ...,
#        "time_in_force": "day" | "gtc" | ...,
#        "limit_price": 170.0,            # optional
#        "stop_price": 169.5,             # optional
#        "trail": null | number,          # adapter-specific
#        "extended_hours": false,
#        "note": "free-form string"
#      }
#
#    Response:
#      { "ok": true, "account_id": "...", "order": OrderDocLike }
#
#    Implementation details:
#      • Preferred path:
#            PortfolioManager.place_order(account_id=..., **args)
#      • Fallback path if that is missing on pm:
#            adapter.place_order(**args)
#        with adapter method name falling back to submit_order/create_order
#        if place_order is not present.
#
# 7) POST /v1/portfolio/reconcile
#      Body: {} or {"account_id": "..."}
#    → Triggers one reconcile pass via pm.reconcile_once(account_id=...).
#      Response mirrors snapshot(s) but is primarily for debugging.
#
# 8) GET /v1/health
#    → { "ok": true, "last_poll_at": "...", "last_poll_error": null, ... }
#      Tracks the background reconcile loop.
#
# Any change to these contracts should be mirrored in BrokerView’s
# cockpit/brokerview/models.py and main.py.
# ---------------------------------------------------------------------------

import os
import time
import random
import asyncio
import logging
import threading
from typing import Dict, Any, Optional, List, Deque
from uuid import uuid4

from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import JSONResponse

from .portfolio_manager import PortfolioManager
from .alpaca_trade_updates import listen_trade_updates

log = logging.getLogger("trader.app")
app = FastAPI(title="Reflex Trader API", version="2.0.0")

# ---------------------------------------------------------------------------
# FastAPI app (defined early so decorators can attach routes)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# FastAPI app (defined early so decorators can attach routes)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# FastAPI app (defined early so decorators can attach routes)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# FastAPI app (defined early so decorators can attach routes)
# ---------------------------------------------------------------------------


# Global portfolio manager instance
pm = PortfolioManager()
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Local Orders Ledger (simple, in-memory)
# ---------------------------------------------------------------------------
# Purpose:
#   1) Immediate local echo (show order the moment user clicks)
#   2) Reconcile to broker truth whenever we see broker orders
#
# This is intentionally simple and lives only in-memory for now.
# ---------------------------------------------------------------------------

_LOCAL_ORDERS: Dict[str, Dict[str, Any]] = {}          # keyed by "id" (client id initially; may become broker id later)
_CLIENT_TO_BROKER: Dict[str, str] = {}                 # client_id -> broker_id

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
    # BrokerView expects these minimum keys.
    return {
        "account_id": o.get("account_id"),
        "id": o.get("id"),
        "symbol": o.get("symbol"),
        "side": o.get("side"),
        "qty": float(o.get("qty") or 0),
        "type": o.get("type"),
        "limit_price": o.get("limit_price"),
        "stop_price": o.get("stop_price"),
        "status": o.get("status"),
        "submitted_at": o.get("submitted_at"),
        "source": o.get("source"),
        "note": o.get("note"),
    }

def _fail_local(client_id: str, aid: str, reason: str, status: str = "failed") -> None:
    """Mark a local order terminal so it clears from Active on reject/failure."""
    try:
        o = _LOCAL_ORDERS.get(client_id)
        if not o:
            return
        o["status"] = status
        o["error"] = reason
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
    Merge a broker OrderDoc into local ledger.
    If we previously created a local order with client id, keep a mapping.
    """
    bid = broker_doc.get("id")
    if not bid:
        return

    # If this broker id matches a client->broker mapping, we can migrate
    # local record key to broker id (so UI stabilizes on broker id).
    # But we do it gently: if we already have broker-id record, update it.
    client_id = None
    for cid, mapped in list(_CLIENT_TO_BROKER.items()):
        if mapped == bid:
            client_id = cid
            break

    existing = _LOCAL_ORDERS.get(bid)
    if not existing and client_id:
        existing = _LOCAL_ORDERS.get(client_id)

    if existing:
        # Merge: broker wins on status/timestamps/prices, local keeps note/source if broker lacks.
        merged = dict(existing)
        merged.update({k: v for k, v in broker_doc.items() if v is not None})
        merged["account_id"] = aid
        merged["id"] = bid
        _LOCAL_ORDERS[bid] = merged
        if client_id and client_id in _LOCAL_ORDERS:
            # keep the old client id record around? No — remove to avoid duplicates.
            _LOCAL_ORDERS.pop(client_id, None)
    else:
        # brand new broker order we never saw locally (external UI, cancel, etc.)
        merged = dict(broker_doc)
        merged["account_id"] = aid
        _LOCAL_ORDERS[bid] = merged

# ---------------------------------------------------------------------------
# Trader Event Hub (SSE)
# ---------------------------------------------------------------------------
# BrokerView should treat these events as the "fast truth" path.
# Polling remains as a drift-correction / safety net.
#
# publish_event() is safe to call from threads (e.g., Alpaca websocket callbacks)
# by scheduling into the main event loop captured at startup.
#
import json as _json
from collections import deque as _deque
from starlette.responses import StreamingResponse as _StreamingResponse

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
    """Publish a Trader event to SSE subscribers (thread-safe)."""
    global _MAIN_LOOP
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

    # small replay buffer
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
# Managed trades (internal bracket / soft-stop foundation)
# ---------------------------------------------------------------------------

# In-memory trade ledger for v1.
# NOTE: This is intentionally simple. Later we can persist to Postgres/Parquet.
_MANAGED_TRADES: Dict[str, Dict[str, Any]] = {}

# Minimal intent ledger (Phase 1). TradeManager/PlanManager will replace this.
_INTENT_HISTORY: Deque[Dict[str, Any]] = _deque(maxlen=500)

# If uvicorn is run with multiple workers, each worker has its own in-memory
# ledgers. When journaling is enabled, we can keep workers coherent by
# reloading the journal when it changes on disk.
_JOURNAL_MTIME: float = 0.0
_JOURNAL_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Trade/Intent Journal (Phase 2.5 – minimal persistence for testability)
# ---------------------------------------------------------------------------
# Why:
#   - In-memory ledgers are wiped on restart.
#   - During Phase 1/2 iteration, crashes/restarts are normal.
#   - A tiny JSONL journal makes the system debuggable without introducing DB
#     migrations or refactors.
#
# Controls:
#   REFLEX__TRADER_JOURNAL=1   -> enable
#   REFLEX__TRADER_JOURNAL_PATH=/path/to/file.jsonl  -> optional override
#
# Default path:
#   <repo_root>/logs/trader/trade_journal.jsonl
# ---------------------------------------------------------------------------

def _truthy_env(name: str, default: str = "0") -> bool:
    v = str(os.getenv(name, default)).strip().lower()
    return v in ("1", "true", "yes", "y", "on")

def _repo_root() -> str:
    # trader/app.py -> trader/ -> <repo_root>
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
        rec = {
            "ts": time.time(),
            "kind": kind,   # "intent" | "trade" | (future) "event"
            "payload": payload,
        }
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

        # Rehydrate ledgers (best-effort). Last write wins.
        for it in intents.values():
            _INTENT_HISTORY.append(it)
        for tid, tr in trades.items():
            _MANAGED_TRADES[tid] = tr

        log.info(
            "journal.replay ok=%s path=%r intents=%d trades=%d",
            True, jp, len(intents), len(trades)
        )
    except Exception:
        log.exception("journal.replay.failed path=%r", jp)



def _journal_sync_if_needed() -> None:
    """Reload journal if it changed on disk (multi-worker coherence).

    With uvicorn --workers > 1, each worker has independent memory. Journaling
    provides a simple shared substrate. This function keeps the in-memory
    ledgers roughly coherent by reloading when the journal mtime increases.
    """
    global _JOURNAL_MTIME
    if not _JOURNAL_ENABLED:
        return
    jp = _journal_path()
    try:
        st = os.stat(jp)
    except FileNotFoundError:
        return
    except Exception:
        return
    mtime = float(getattr(st, 'st_mtime', 0.0) or 0.0)
    if mtime <= _JOURNAL_MTIME:
        return
    with _JOURNAL_LOCK:
        # re-check after acquiring lock
        try:
            st2 = os.stat(jp)
            mtime2 = float(getattr(st2, 'st_mtime', 0.0) or 0.0)
        except Exception:
            return
        if mtime2 <= _JOURNAL_MTIME:
            return
        # replay appends into current ledgers (last write wins)
        _journal_replay()
        _JOURNAL_MTIME = mtime2

# replay at import time so a restart keeps your Phase 1/2 state
_journal_replay()


def _new_trade_id() -> str:
    return str(uuid4())


def _new_intent_id() -> str:
    return str(uuid4())


def _normalize_intent(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize intent payload (Phase 1).

    Intent is a request. It should carry *what*/*when* (symbol/side/trigger) plus
    optional hints. It should not carry executable broker recipes.
    """
    sym = str(data.get("symbol", "")).upper().strip()
    side = str(data.get("side", "buy")).lower().strip() or "buy"
    source = str(data.get("source", data.get("channel", "bot"))).lower().strip() or "bot"
    strategy_id = str(data.get("strategy_id", data.get("model_profile", ""))).strip()

    trigger = data.get("trigger")
    if not trigger:
        # minimal default: immediate
        trigger = {"kind": "immediate"}

    return {
        "intent_id": _new_intent_id(),
        "created_ts": time.time(),
        "symbol": sym,
        "side": side,
        "source": source,
        "strategy_id": strategy_id,
        "trigger": trigger,
        "reason": data.get("reason") or data.get("notes") or "",
        "raw": data,
    }


def _default_account_id() -> Optional[str]:
    """Pick a default account id when only one adapter exists."""
    try:
        if len(pm.adapters) == 1:
            return next(iter(pm.adapters.keys()))
    except Exception:
        pass
    return None


def _as_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _pick_default_account_id() -> str:
    """Choose a default account id when caller doesn't specify one.

    Phase 1 behavior:
      - If exactly one adapter is registered, use it.
      - Otherwise, require the caller to pass account_id.
    """
    if len(pm.adapters) == 1:
        return next(iter(pm.adapters.keys()))
    raise HTTPException(status_code=400, detail="account_id required")


# ---------------------------------------------------------------------------
# Phase 2 (minimal): RiskDecision + TradePlan compilation
# ---------------------------------------------------------------------------


def _risk_decision_v1(account_id: str, symbol: str, side: str, raw_intent: Dict[str, Any]) -> Dict[str, Any]:
    """Very small RiskManager stub (Phase 2).

    Purpose: prove the wiring and responsibility boundaries.
    - Authorizes a fixed size
    - Defines a max_total_risk envelope
    - Disables add-ons by default

    This does NOT place orders.
    """
    # Allow overrides for manual only (still bounded). Keep conservative.
    src = str(raw_intent.get("source") or "bot").lower()
    requested_shares = raw_intent.get("shares") or raw_intent.get("qty")
    shares = 10
    try:
        if src == "manual" and requested_shares is not None:
            shares = max(1, min(int(requested_shares), 1000))
    except Exception:
        shares = 10

    # Envelope (simple placeholder).
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
    """Very small PlanManager stub (Phase 2).

    Produces a declarative TradePlan (entry + exit placeholders).
    Runtime execution is a later phase.
    """
    trigger = intent_doc.get("trigger") or {"kind": "immediate"}
    # Normalize trigger shape between earlier {"type":...} and desired {"kind":...}
    if isinstance(trigger, dict) and "type" in trigger and "kind" not in trigger:
        trigger = {**trigger, "kind": trigger.get("type")}

    entry_plan = {
        "kind": "entry",
        "style": "market",
        "trigger": trigger,
        "shares": risk.get("shares"),
    }

    # Phase 2 exit plan is a placeholder: runtime will enforce protection later.
    exit_plan = {
        "kind": "exit",
        "hard_stop": {"mode": "required_but_unimplemented"},
        "soft_stop": {"mode": "disabled"},
    }

    scale_plan = {
        "mode": "disabled",
        "tranches": [],
        "notes": "phase2",
    }

    return {
        "version": 1,
        "entry": entry_plan,
        "exit": exit_plan,
        "scale": scale_plan,
    }


@app.get("/v1/intents")
async def api_list_intents(limit: int = Query(50, alias="limit")):
    """List recent intents (Phase 1).

    This is intentionally simple: it exists to validate Intent → Trade wiring.
    """
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
    """Submit an intent and create a PLANNED trade (Phase 1).

    Bot and manual intents share the same shape. Manual may provide overrides,
    but **no orders are placed in Phase 1**. This endpoint only validates the
    plumbing and creates a trade stub visible via /v1/trades?status=planned.
    """
    try:
        data = await request.json()
    except Exception:
        data = {}

    account_id = (data.get("account_id") or "").strip() or None
    if not account_id:
        account_id = _pick_default_account_id()
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
    trigger = data.get("trigger") or {"type": "immediate"}

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

    # ------------------------------
    # Phase 2: attach Risk + Plan
    # ------------------------------
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
    }
    _MANAGED_TRADES[trade_id] = trade_doc
    _journal_append("trade", trade_doc)

    publish_event({
        "type": "INTENT_ACCEPTED",
        "ts": now,
        "account_id": account_id,
        "symbol": symbol,
        "trade_id": trade_id,
        "intent_id": intent_id,
        "source": source,
        "strategy_id": strategy_id,
        "state": "READY",
    })

    publish_event({
        "type": "TRADE_READY",
        "ts": now,
        "account_id": account_id,
        "symbol": symbol,
        "trade_id": trade_id,
        "intent_id": intent_id,
        "risk": {"shares": risk.get("shares"), "max_total_risk_$": risk.get("max_total_risk_$")},
        "plan": {"entry": plan.get("entry"), "exit": plan.get("exit")},
    })

    return {"ok": True, "intent": intent_doc, "trade": trade_doc}


def _guard_manual_order(data: Dict[str, Any], snapshot: Optional[Dict[str, Any]]) -> None:
    """Basic "fumble fingers" protections.

    This is intentionally conservative. It should *reject* unsafe orders rather
    than guessing.
    """

    qty = _as_float(data.get("qty")) or 0.0
    if qty <= 0:
        raise HTTPException(status_code=400, detail="qty must be > 0")

    # Hard caps from env
    per_order_usd = _as_float(os.getenv("TRADER_PER_ORDER_USD_CAP", "500")) or 500.0
    price_band_pct = _as_float(os.getenv("TRADER_PRICE_BAND_PCT", "5")) or 5.0

    side = str(data.get("side", "")).lower().strip()
    if side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side must be 'buy' or 'sell'")

    # Accidental short protection (v1): do not allow SELL if no long position.
    if side == "sell":
        sym = str(data.get("symbol", "")).upper().strip()
        allow_short = bool(data.get("allow_short", False))
        if not allow_short and snapshot:
            pos = None
            for p in (snapshot.get("positions") or []):
                if str(p.get("symbol", "")).upper() == sym:
                    pos = p
                    break
            if not pos or float(pos.get("qty") or 0) <= 0:
                raise HTTPException(status_code=400, detail="refusing to sell without a long position (allow_short=false)")

    # Price sanity for limit orders
    otype = str(data.get("type", "market")).lower().strip()
    limit_price = _as_float(data.get("limit_price"))
    if otype in ("limit", "stop_limit") and (limit_price is None or limit_price <= 0):
        raise HTTPException(status_code=400, detail="limit_price required for limit/stop_limit")

    # Notional + band checks if we have market price
    market_price = None
    if snapshot and data.get("symbol"):
        sym = str(data.get("symbol", "")).upper().strip()
        for p in (snapshot.get("positions") or []):
            if str(p.get("symbol", "")).upper() == sym:
                mp = _as_float(p.get("market_price"))
                if mp and mp > 0:
                    market_price = mp
                break

    ref_price = limit_price or market_price
    if ref_price and ref_price > 0:
        notional = qty * ref_price
        if notional > per_order_usd:
            raise HTTPException(status_code=400, detail=f"order notional ${notional:,.2f} exceeds cap ${per_order_usd:,.2f}")

        if market_price and limit_price:
            dev = abs(limit_price - market_price) / market_price * 100.0
            if dev > price_band_pct:
                raise HTTPException(status_code=400, detail=f"limit_price is {dev:.2f}% away from market; band={price_band_pct:.2f}%")


def _find_position(snapshot: Dict[str, Any], symbol: str) -> Optional[Dict[str, Any]]:
    sym = (symbol or "").upper().strip()
    for p in (snapshot.get("positions") or []):
        if str(p.get("symbol", "")).upper() == sym:
            return p
    return None


async def _managed_trades_tick() -> None:
    """Advance managed trades based on latest portfolio snapshots.

    v1 rules:
      - Long-only managed BUY trades
      - Bracket: stop_loss + take_profit (absolute prices)
      - Exit is either market (regular) or marketable limit (extended hours)
    """

    if not _MANAGED_TRADES:
        return

    # For each trade: detect fill via position presence; then monitor price.
    for tid, t in list(_MANAGED_TRADES.items()):
        try:
            if t.get("state") in ("DONE", "CANCELED", "ERROR"):
                continue

            aid = t.get("account_id")
            sym = t.get("symbol")
            if not aid or not sym:
                t["state"] = "ERROR"
                t["error"] = "missing account_id/symbol"
                continue

            snap = pm.get_snapshots().get(aid)  # already dict
            if not snap:
                continue

            pos = _find_position(snap, sym)
            qty = float(t.get("qty") or 0)

            if t.get("state") == "ENTERING":
                if pos and float(pos.get("qty") or 0) >= qty and float(pos.get("avg_price") or 0) > 0:
                    t["state"] = "OPEN"
                    t["entry_avg_price"] = float(pos.get("avg_price") or 0)
                    # If bracket configured in offsets, resolve now
                    if t.get("stop_loss_cents") is not None and t.get("stop_price") is None:
                        t["stop_price"] = t["entry_avg_price"] - (float(t["stop_loss_cents"]) / 100.0)
                    if t.get("take_profit_cents") is not None and t.get("take_profit_price") is None:
                        t["take_profit_price"] = t["entry_avg_price"] + (float(t["take_profit_cents"]) / 100.0)

            if t.get("state") == "OPEN":
                # If position disappears, assume closed externally
                if not pos or float(pos.get("qty") or 0) <= 0:
                    t["state"] = "DONE"
                    continue

                mp = _as_float(pos.get("market_price"))
                if mp is None or mp <= 0:
                    # No market price; cannot evaluate bracket safely.
                    continue

                stop_price = _as_float(t.get("stop_price"))
                tp_price = _as_float(t.get("take_profit_price"))

                trigger: Optional[str] = None
                if stop_price is not None and mp <= stop_price:
                    trigger = "STOP_LOSS"
                elif tp_price is not None and mp >= tp_price:
                    trigger = "TAKE_PROFIT"

                if trigger and not t.get("exit_order_id"):
                    adapter = pm.adapters.get(aid)
                    if adapter is None:
                        continue
                    extended = bool(t.get("extended_hours", False))

                    # Exit long by selling qty.
                    exit_type = "market"
                    payload: Dict[str, Any] = {
                        "symbol": sym,
                        "side": "sell",
                        "qty": qty,
                        "type": "market",
                        "time_in_force": "day",
                        "extended_hours": extended,
                        "note": f"managed_exit:{trigger}:trade_id={tid}",
                    }

                    if extended:
                        # Extended-hours market orders are typically rejected; use a marketable limit.
                        exit_type = "limit"
                        payload["type"] = "limit"
                        payload["limit_price"] = round(mp * 0.995, 2)  # sell slightly below last

                    res = adapter.place_order(**payload)
                    t["exit_order_id"] = res.get("id") or res.get("client_order_id") or res.get("order_id")
                    t["exit_reason"] = trigger
                    t["state"] = "EXITING"

            if t.get("state") == "EXITING":
                # Mark done when position closed
                if not pos or float(pos.get("qty") or 0) <= 0:
                    t["state"] = "DONE"

        except Exception as e:
            t["state"] = "ERROR"
            t["error"] = str(e)


# ---------------------------------------------------------------------------
# Backwards-compat: reconcile_once shim
# ---------------------------------------------------------------------------

if not hasattr(pm, "reconcile_once"):
    async def _reconcile_once_shim(account_id: Optional[str] = None) -> None:
        """
        Provide pm.reconcile_once(account_id=...) for older PortfolioManager
        implementations that only expose reconcile()/reconcile_all().
        """
        target = None
        for name in ("reconcile", "reconcile_all"):
            fn = getattr(pm, name, None)
            if fn is not None:
                target = fn
                break

        if target is None:
            raise RuntimeError(
                "PortfolioManager has no reconcile_once/reconcile/reconcile_all; "
                "cannot perform portfolio reconciliation."
            )

        if asyncio.iscoroutinefunction(target):
            try:
                if account_id is not None:
                    return await target(account_id=account_id)
            except TypeError:
                return await target()
        else:
            try:
                if account_id is not None:
                    return target(account_id=account_id)
            except TypeError:
                return target()

    setattr(pm, "reconcile_once", _reconcile_once_shim)  # type: ignore[attr-defined]
    log.info("portfolio_manager.reconcile_once.shim_installed")

# ---------------------------------------------------------------------------
# Background reconcile loop configuration
# ---------------------------------------------------------------------------

POLL_SECS = float(os.getenv("TRADER_RECONCILE_INTERVAL", "10"))
_LAST_POLL_TS: float = 0.0
_LAST_POLL_ERR: Optional[str] = None
_bg_task: Optional[asyncio.Task] = None


def _last_poll_iso() -> Optional[str]:
    if not _LAST_POLL_TS:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(_LAST_POLL_TS))


# ---------------------------------------------------------------------------
# Helper: normalize positions and orders
# ---------------------------------------------------------------------------

def _coerce_positions(raw_positions: List[Any], account_id: str) -> List[Dict[str, Any]]:
    """
    Convert adapter/PortfolioManager Position objects into PositionDoc dicts
    that match the BrokerView contract (see header).
    """
    norm: List[Dict[str, Any]] = []
    for p in raw_positions or []:
        if hasattr(p, "to_dict"):
            d = p.to_dict()
        elif isinstance(p, dict):
            d = dict(p)
        else:
            d = {"raw": repr(p)}

        # Normalize key names and defaults
        sym = d.get("symbol") or d.get("asset_id") or d.get("asset") or d.get("ticker")
        qty = d.get("qty", d.get("quantity", 0) or 0) or 0
        avg_price = d.get("avg_price", d.get("avg_fill_price", d.get("cost_basis", 0) or 0))
        mkt_price = d.get("market_price", d.get("current_price", d.get("last_price", 0) or 0))
        side = d.get("side") or ("long" if qty >= 0 else "short")

        out = {
            "account_id": account_id,
            "symbol": sym,
            "qty": float(qty),
            "avg_price": float(avg_price or 0),
            "market_price": float(mkt_price or 0),
            "side": side,
        }
        norm.append(out)
    return norm


def _coerce_order_dict(o: Any, account_id: str) -> Dict[str, Any]:
    """
    Normalize an adapter order object into the OrderDoc shape used by BrokerView.
    """
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
    submitted_at = (
        d.get("submitted_at")
        or d.get("created_at")
        or d.get("timestamp")
    )

    return {
        "account_id": account_id,
        "id": order_id,
        "symbol": sym,
        "side": d.get("side"),
        "qty": float(qty),
        "type": d.get("type"),
        "limit_price": d.get("limit_price"),
        "stop_price": d.get("stop_price"),
        "status": status,
        "submitted_at": submitted_at,
    }


def _snapshot_for_account(aid: str) -> Dict[str, Any]:
    s = pm.portfolio.get(aid)
    if s is None:
        return {
            "account_id": aid,
            "cash": 0.0,
            "equity": 0.0,
            "buying_power": 0.0,
            "positions": [],
            "updated_at": None,
        }
    # PortfolioManager may already store normalized positions; we still coerce.
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


async def _list_orders_for_account(aid: str, status: str) -> List[Dict[str, Any]]:
    """
    Ask the adapter for orders with the given status and normalize them.

    status in {"active", "closed"}:
      • "active"  → adapter.list_open_orders()
      • "closed"  → adapter.list_closed_orders()  (if present)
    """
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
        orders = await func() if asyncio.iscoroutinefunction(func) else func()
        return [_coerce_order_dict(o, aid) for o in (orders or [])]
    except Exception:
        log.exception("list_orders.failed", extra={"account_id": aid, "status": status})
        return []


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------



@app.get("/")
async def root():
    """Simple landing page for sanity checks."""
    return {
        "ok": True,
        "service": "Reflex Trader API",
        "adapters": list(pm.adapters.keys()),
        "portfolio_accounts": list(pm.portfolio.keys()),
    }


# ---------------------------------------------------------------------------
# Portfolio endpoints
# ---------------------------------------------------------------------------


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
    """
    Overview payload used by BrokerView's Accounts tab.

    See header comments for exact expected shape.
    """
    overview: Dict[str, Any] = {}

    for aid, adapter in pm.adapters.items():
        snap = _snapshot_for_account(aid)
        balances = {
            "cash": snap["cash"],
            "equity": snap["equity"],
            "buying_power": snap["buying_power"],
            "updated_at": snap["updated_at"],
        }
        positions = snap["positions"]
        open_orders = await _list_orders_for_account(aid, status="active")

        broker_id = getattr(adapter, "broker_id", None) or getattr(adapter, "broker", None)
        kind = (
            getattr(adapter, "kind", None)
            or getattr(adapter, "adapter_kind", None)
            or getattr(adapter, "name", None)
            or "unknown"
        )

        overview[aid] = {
            "balances": balances,
            "positions": positions,
            "open_orders": open_orders,
            "broker_id": broker_id,
            "kind": kind,
        }

    return {
        "overview": overview,
        "count": len(overview),
        "live_only": False,
        "account_id": None,
    }


@app.get("/v1/portfolio/positions")
async def api_positions(account_id: Optional[str] = Query(None, alias="account_id")):
    """
    Return positions as a FLAT LIST of PositionDoc dicts.

    BrokerView expects:
      { "ok": true, "positions": [ PositionDoc, ... ] }
    """
    positions: List[Dict[str, Any]] = []

    if account_id:
        snap = _snapshot_for_account(account_id)
        positions.extend(snap["positions"])
    else:
        for aid in pm.adapters.keys():
            snap = _snapshot_for_account(aid)
            positions.extend(snap["positions"])

    return {"ok": True, "positions": positions}


@app.post("/v1/portfolio/reconcile")
async def api_reconcile(request: Request):
    """
    Trigger an on-demand reconcile loop. Optional body:
      { "account_id": "alpaca:paper" } or {} for all.
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    aid = data.get("account_id")

    try:
        await pm.reconcile_once(account_id=aid)  # type: ignore[attr-defined]
    except Exception as e:
        log.exception("reconcile_once.failed", extra={"account_id": aid})
        raise HTTPException(status_code=500, detail=str(e)) from e

    if aid:
        snapshot = _snapshot_for_account(aid)
        return {
            "ok": True,
            "message": "reconciled",
            "accounts": [aid],
            "results": {aid: {"status": "ok", "snapshot": snapshot}},
        }

    results: Dict[str, Any] = {}
    for k in pm.adapters.keys():
        results[k] = {"status": "ok", "snapshot": _snapshot_for_account(k)}

    return {
        "ok": True,
        "message": "reconciled",
        "accounts": list(pm.adapters.keys()),
        "results": results,
    }


@app.post("/v1/portfolio/flatten")
async def api_flatten(request: Request):
    """Flatten positions for a given account_id (or all accounts)."""
    try:
        data = await request.json()
    except Exception:
        data = {}

    aid = data.get("account_id")

    publish_event({"type": "FLATTEN_REQUEST", "account_id": aid, "ts": time.time()})
    try:
        await pm.flatten(account_id=aid)
        # force reconcile right after flatten request
        if aid:
            await pm.reconcile_account(aid)
        else:
            await pm.reconcile_once()
        publish_event({"type": "FLATTEN_DONE", "account_id": aid, "ts": time.time()})
        return {"ok": True, "account_id": aid}
    except Exception as e:
        log.exception("flatten.failed", extra={"account_id": aid})
        publish_event({"type": "FLATTEN_FAILED", "account_id": aid, "error": str(e), "ts": time.time()})
        _fail_local(client_id, aid, str(e))
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


async def _background_reconciler():
    """
    Safety-net reconciler. Broker WS events should drive truth updates, but this
    loop heals missed events / transient failures.
    """
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
            if hasattr(pm, "reconcile_once"):
                await pm.reconcile_once()  # type: ignore[attr-defined]
            _LAST_POLL_TS = time.time()
            _LAST_POLL_ERR = None
        except Exception as e:
            _LAST_POLL_TS = time.time()
            _LAST_POLL_ERR = str(e)
            log.exception("background_reconcile.failed")

        # Managed trades (soft exits / brackets) tick after reconcile
        try:
            await _managed_trades_tick()
        except Exception:
            log.exception("managed_trades.tick.failed")


async def _background_broker_events():
    """
    Alpaca trade update stream -> immediate reconcile. This is the "fast truth" path.
    """
    global _bg_events_stop, _bg_events_tasks
    _bg_events_stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    tasks = []
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
                    try:
                        if hasattr(pm, "reconcile_account"):
                            asyncio.run_coroutine_threadsafe(pm.reconcile_account(account_id), loop)  # type: ignore[attr-defined]
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

@app.on_event("startup")
async def on_startup():
    global POLL_SECS, _LAST_POLL_TS, _LAST_POLL_ERR, _bg_task
    log.info("startup.begin")
    global _MAIN_LOOP
    _MAIN_LOOP = asyncio.get_running_loop()
    try:
        await pm.register_from_db()
    except Exception:
        log.exception("startup.register_from_db.failed")
        raise

    # IMPORTANT: populate an initial snapshot immediately so BrokerView doesn't
    # stare at zeros / "Loading..." until the first background tick.
    try:
        await pm.reconcile_once()  # type: ignore[attr-defined]
        _LAST_POLL_TS = time.time()
        _LAST_POLL_ERR = None
    except Exception as e:
        _LAST_POLL_TS = time.time()
        _LAST_POLL_ERR = str(e)
        log.exception("startup.initial_reconcile.failed")

    POLL_SECS = max(3.0, min(POLL_SECS, 30.0))
    # _LAST_POLL_* already set above (or left as failure state)

    loop = asyncio.get_event_loop()
    _bg_task = loop.create_task(_background_reconciler())
    # Broker event listeners (fast truth updates)
    loop.create_task(_background_broker_events())
    log.info("startup.done")


@app.on_event("shutdown")
async def on_shutdown():
    global _bg_task, _bg_events_tasks, _bg_events_stop
    log.info("shutdown.begin")

    # Stop event listeners
    try:
        if _bg_events_stop is not None:
            _bg_events_stop.set()
    except Exception:
        pass

    # Cancel reconcile loop
    if _bg_task:
        try:
            _bg_task.cancel()
            try:
                await _bg_task
            except asyncio.CancelledError:
                pass
        except Exception:
            pass

    _bg_task = None
    _bg_events_tasks = []
    _bg_events_stop = None
    log.info("shutdown.done")


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
    }



@app.get("/v1/events")
async def stream_events(request: Request):
    """Server-Sent Events (SSE) stream of Trader events."""
    headers = {"Cache-Control": "no-cache", "Connection": "keep-alive"}
    return _StreamingResponse(_sse_event_generator(request), media_type="text/event-stream", headers=headers)


@app.post("/v1/portfolio/config")
async def api_config(request: Request):
    global POLL_SECS, _bg_task
    try:
        data = await request.json()
    except Exception:
        data = {}
    new_val = data.get("poll_seconds")
    if new_val is not None:
        try:
            POLL_SECS = float(new_val)
        except ValueError:
            raise HTTPException(status_code=400, detail="poll_seconds must be a number")

        if _bg_task:
            _bg_task.cancel()
            try:
                await _bg_task
            except asyncio.CancelledError:
                pass

        loop = asyncio.get_event_loop()
        _bg_task = loop.create_task(_background_reconciler())
    # Broker event listeners (fast truth updates)
    loop.create_task(_background_broker_events())

    return {"ok": True, "poll_seconds": POLL_SECS, "last_poll_at": _last_poll_iso()}


# ---------------------------------------------------------------------------
# Orders API
# ---------------------------------------------------------------------------


@app.get("/v1/orders")
async def api_list_orders(
    status: str = Query("active", alias="status"),
    account_id: Optional[str] = Query(None, alias="account_id"),
):
    """
    BrokerView compat: GET /v1/orders?status=active|closed[&account_id=...]

    Returns:
      - local ledger orders (immediate echo)
      - plus broker truth (and merges broker into local on read)
    """
    if status not in ("active", "closed"):
        status = "active"

    # 1) Pull broker orders (truth) and merge into local ledger
    broker_orders: List[Dict[str, Any]] = []
    if account_id:
        broker_orders = await _list_orders_for_account(account_id, status=status)
        for od in broker_orders:
            _upsert_local_from_broker(account_id, od)
    else:
        for aid in pm.adapters.keys():
            ods = await _list_orders_for_account(aid, status=status)
            broker_orders.extend(ods)
            for od in ods:
                _upsert_local_from_broker(aid, od)

    # 2) Collect local ledger filtered by status/account
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

    # Stable sort: newest first by submitted_at if present
    def _sort_key(x: Dict[str, Any]) -> str:
        return str(x.get("submitted_at") or "")
    out.sort(key=_sort_key, reverse=True)

    return {"ok": True, "orders": out}



@app.post("/v1/orders")
@app.post("/v1/orders/place")
async def api_orders_place(request: Request):
    """
    BrokerView & intent bridge: place a new order.

    Preferred path: PortfolioManager.place_order(account_id=..., **args)
    Fallback: adapter.place_order/submit_order/create_order(**args)
    """
    try:
        data = await request.json()
    except Exception:
        data = {}



    aid = data.get("account_id")
    if not aid:
        if len(pm.adapters) == 1:
            aid = next(iter(pm.adapters.keys()))
        else:
            log.error(
                "orders.missing_account_id",
                extra={"adapters": list(pm.adapters.keys())},
            )
            return JSONResponse(
                {
                    "ok": False,
                    "error": "account_id required when multiple accounts present",
                },
                status_code=400,
            )

    if aid not in pm.adapters:
        log.error(
            "orders.unknown_account_id",
            extra={"account_id": aid, "adapters": list(pm.adapters.keys())},
        )
        return JSONResponse(
            {"ok": False, "error": "unknown account_id"},
            status_code=404,
        )

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
        "note": data.get("note") or "testing",
    }


    # ------------------------------------------------------------------
    # Local echo: create a local order record immediately (before broker)
    # ------------------------------------------------------------------
    client_id = str(uuid4())
    submitted_at = _iso_now()

    note = args.get("note") or ""
    # Tag note with client id for traceability (safe to pass through everywhere)
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
        "limit_price": args.get("limit_price"),
        "stop_price": args.get("stop_price"),
        "status": "pending_local",
        "submitted_at": submitted_at,
        "source": "manual",
        "note": note,
    }
    
    _LOCAL_ORDERS[client_id] = local

    publish_event({"type": "ORDER_LOCAL_NEW", "ts": time.time(), "account_id": aid, "order": _local_order_doc(local)})
    


    # Trader-only control flags (never passed to broker adapters)
    allow_short = bool(data.get("allow_short", False))

    # Snapshot for guardrails (may be slightly stale, but prevents obvious mistakes)
    snap = _snapshot_for_account(aid)
    if args["side"] == "sell" and not allow_short:
        sym_u = (args.get("symbol") or "").strip().upper()
        req_qty = float(args.get("qty") or 0)
        pos_qty = 0.0
        for p in snap.get("positions") or []:
            if (p.get("symbol") or "").upper() == sym_u:
                pos_qty = float(p.get("qty") or 0)
                break
        if req_qty > pos_qty + 1e-9:
            reason = f"sell qty {req_qty:g} exceeds position qty {pos_qty:g} (short not allowed)"
            _fail_local(client_id, aid, reason)
            return JSONResponse({"ok": False, "error": reason}, status_code=400)


    # Basic guardrails for human/manual entry. Auto flows can bypass by setting
    # `guard=false` explicitly, but BrokerView should leave it on.
    guard_on = bool(data.get("guard", True))
    if guard_on:
        snap = pm.get_snapshots().get(aid)
        try:
            _guard_manual_order({**args, "account_id": aid, "allow_short": bool(data.get("allow_short", False))}, snap)
        except HTTPException as e:
            _fail_local(client_id, aid, str(e.detail))
            raise
    if not args["symbol"] or not args["side"] or not args["qty"]:
        log.error(
            "orders.missing_required_fields",
            extra={"account_id": aid, "order_args": args},
        )
        _fail_local(client_id, aid, "symbol, side, qty required")
        return JSONResponse({"ok": False, "error": "symbol, side, qty required"}, status_code=400)

        # Broker adapters must NOT receive Trader-only control flags.
    broker_args = dict(args)
    broker_args.pop("allow_short", None)

# ------------------------------------------------------------------
    # First choice: PortfolioManager.place_order(...)
    # ------------------------------------------------------------------
    pm_fn = getattr(pm, "place_order", None)
    if pm_fn is not None:
        try:
            log.info(
                "orders.pm_place_order",
                extra={"account_id": aid, "order_args": args},
            )
            if asyncio.iscoroutinefunction(pm_fn):
                res = await pm_fn(account_id=aid, **broker_args)
            else:
                res = pm_fn(account_id=aid, **broker_args)
            log.info(
                "orders.pm_place_order.ok",
                extra={"account_id": aid, "result": res},
            )
            return {"ok": True, "account_id": aid, "order": res}
        except Exception as e:
            log.exception(
                "orders.pm_place_order.failed",
                extra={"account_id": aid},
            )
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    # ------------------------------------------------------------------
    # Fallback: adapter.place_order/submit_order/create_order(...)
    # ------------------------------------------------------------------
    ad = pm.adapters[aid]
    adapter_type = type(ad).__name__

    fn = getattr(ad, "place_order", None)
    method_name = "place_order"
    if fn is None:
        for alt in ("submit_order", "create_order"):
            alt_fn = getattr(ad, alt, None)
            if alt_fn is not None:
                fn = alt_fn
                method_name = alt
                break

    if fn is None:
        methods = [m for m in dir(ad) if not m.startswith("_")]
        log.error(
            "orders.adapter_missing_method",
            extra={
                "account_id": aid,
                "adapter_type": adapter_type,
                "methods": methods,
            },
        )
        return JSONResponse(
            {"ok": False, "error": "adapter does not support place_order"},
            status_code=400,
        )

    try:
        log.info(
            "orders.adapter_place_order",
            extra={
                "account_id": aid,
                "adapter_type": adapter_type,
                "method": method_name,
                "order_args": args,
            },
        )
        if asyncio.iscoroutinefunction(fn):
            res = await fn(**broker_args)
        else:
            res = fn(**broker_args)
        log.info(
            "orders.adapter_place_order.ok",
            extra={"account_id": aid, "result": res},
        )
        return {"ok": True, "account_id": aid, "order": res}
    except Exception as e:
        log.exception(
            "orders.adapter_place_order.failed",
            extra={"account_id": aid, "adapter_type": adapter_type},
        )
        _fail_local(client_id, aid, str(e))
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Trades API (managed brackets)
# ---------------------------------------------------------------------------



@app.post("/v1/orders/cancel_all")
async def api_orders_cancel_all(request: Request):
    """Cancel all open orders for an account."""
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

    # Prefer adapter native cancel-all if available
    fn = getattr(ad, "cancel_all_orders", None) or getattr(ad, "cancel_all", None)
    try:
        if fn is not None:
            if asyncio.iscoroutinefunction(fn):
                await fn()
            else:
                fn()
        else:
            # Fallback: list open orders and cancel one by one
            open_orders = _list_orders_for_account(aid, status="active")
            cancel_one = getattr(ad, "cancel_order", None) or getattr(ad, "cancel", None)
            if cancel_one is None:
                return JSONResponse({"ok": False, "error": "adapter has no cancel method"}, status_code=400)
            for o in open_orders:
                oid = o.get("id") or o.get("order_id")
                if not oid:
                    continue
                if asyncio.iscoroutinefunction(cancel_one):
                    await cancel_one(oid)
                else:
                    cancel_one(oid)
        # reconcile immediately after cancel burst
        await pm.reconcile_account(aid)
        publish_event({"type": "CANCEL_ALL_DONE", "account_id": aid, "ts": time.time()})
        return {"ok": True, "account_id": aid}
    except Exception as e:
        publish_event({"type": "CANCEL_ALL_FAILED", "account_id": aid, "error": str(e), "ts": time.time()})
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/v1/intents")
async def api_list_intents(limit: int = Query(50, alias="limit")):
    """List recently received intents (Phase 1).

    This is a small diagnostic ledger so we can prove intent flow without
    placing orders yet.
    """
    try:
        n = int(limit)
    except Exception:
        n = 50
    n = max(1, min(200, n))
    items = list(_INTENT_HISTORY)[-n:]
    return {"ok": True, "intents": items}


@app.post("/v1/intents")
async def api_submit_intent(request: Request):
    """Submit an intent (bot or manual) and create a PLANNED trade (Phase 1).

    Phase 1 behavior:
      - Normalize the intent
      - Create a trade shell with state=PLANNED
      - Publish an event for Cockpit
      - Do NOT place any broker orders yet
    """
    try:
        data = await request.json()
    except Exception:
        data = {}

    intent = _normalize_intent(data)
    if not intent.get("symbol"):
        raise HTTPException(status_code=400, detail="symbol is required")

    # Account routing: prefer explicit account_id; otherwise use single-adapter default.
    aid = data.get("account_id") or _default_account_id()
    if not aid:
        raise HTTPException(status_code=400, detail="account_id required (multiple accounts configured)")
    if aid not in pm.adapters:
        raise HTTPException(status_code=404, detail="unknown account_id")

    trade_id = _new_trade_id()
    trade = {
        "trade_id": trade_id,
        "created_ts": time.time(),
        "state": "PLANNED",
        "account_id": aid,
        "symbol": intent.get("symbol"),
        "side": intent.get("side"),
        "strategy_id": intent.get("strategy_id"),
        "source": intent.get("source"),
        "trigger": intent.get("trigger"),
        "intent": intent,
        # placeholders for later phases
        "risk": {},
        "plan": {},
        "events": [],
    }
    _MANAGED_TRADES[trade_id] = trade

    # Append intent to debug ledger
    _INTENT_HISTORY.append(intent)

    publish_event({
        "type": "INTENT_ACCEPTED",
        "ts": time.time(),
        "trade_id": trade_id,
        "intent_id": intent.get("intent_id"),
        "account_id": aid,
        "symbol": intent.get("symbol"),
        "side": intent.get("side"),
        "source": intent.get("source"),
        "strategy_id": intent.get("strategy_id"),
        "state": "PLANNED",
    })

    return {"ok": True, "trade": trade}


@app.get("/v1/trades")
async def api_list_trades(status: str = Query("active", alias="status"), account_id: Optional[str] = Query(None, alias="account_id")):
    """List managed trades.

    status: planned|active|closed
      - planned = PLANNED
      - active  = ENTERING/OPEN/EXITING
      - closed  = DONE/CANCELED/ERROR
    """
    _journal_sync_if_needed()
    if status not in ("planned", "active", "closed"):
        status = "active"

    # Phase 2 introduces READY (compiled risk+plan, not executed yet)
    planned_states = {"PLANNED", "READY"}
    active_states = {"ENTERING", "OPEN", "EXITING"}
    closed_states = {"DONE", "CANCELED", "ERROR"}

    out: List[Dict[str, Any]] = []
    for t in _MANAGED_TRADES.values():
        if account_id and t.get("account_id") != account_id:
            continue
        st = t.get("state")
        if status == "planned" and st in planned_states:
            out.append(t)
        elif status == "active" and st in active_states:
            out.append(t)
        elif status == "closed" and st in closed_states:
            out.append(t)

    # stable ordering: newest first
    out.sort(key=lambda x: float(x.get("created_ts") or 0), reverse=True)
    return {"ok": True, "trades": out}


# ---------------------------------------------------------------------------
# Intents (Phase 1) — accept intent, create PLANNED trade (no order placement)
# ---------------------------------------------------------------------------



@app.get("/v1/trades/{trade_id}")
async def api_get_trade(trade_id: str):
    """Fetch a single managed trade by trade_id."""
    _journal_sync_if_needed()
    t = _MANAGED_TRADES.get(trade_id)
    if not t:
        raise HTTPException(status_code=404, detail="trade not found")
    return {"ok": True, "trade": t}

@app.get("/v1/intents")
async def api_list_intents(limit: int = Query(100, alias="limit")):
    """List recently received intents (Phase 1).

    This is for wiring/verification only; it is not a long-term datastore.
    """
    _journal_sync_if_needed()
    try:
        n = max(1, min(int(limit), 500))
    except Exception:
        n = 100
    items = list(_INTENT_HISTORY)[-n:]
    items.reverse()  # newest first
    return {"ok": True, "intents": items}


@app.post("/v1/intents")
async def api_submit_intent(request: Request):
    """Accept an intent and create a PLANNED trade.

    Phase 1 scope:
      - create trade_id
      - store trade in memory with state=PLANNED
      - publish events so BrokerView can show it
      - DO NOT place any broker orders yet

    Later phases:
      - TradeManager will compile plans and execute entries/exits
    """
    try:
        data = await request.json()
    except Exception:
        data = {}

    intent = _normalize_intent(data or {})

    # Determine account
    aid = str((data or {}).get("account_id") or (data or {}).get("account") or "").strip() or None
    if aid is None:
        aid = _default_account_id()
    if aid is None:
        raise HTTPException(status_code=400, detail="account_id required (multiple accounts configured)")

    if aid not in pm.adapters:
        raise HTTPException(status_code=404, detail=f"unknown account_id: {aid}")

    if not intent.get("symbol"):
        raise HTTPException(status_code=400, detail="symbol required")

    trade_id = _new_trade_id()
    trade = {
        "trade_id": trade_id,
        "created_ts": time.time(),
        "state": "PLANNED",
        "account_id": aid,
        "symbol": intent["symbol"],
        "side": intent.get("side"),
        "strategy_id": intent.get("strategy_id"),
        "intent": intent,
        "notes": intent.get("reason", ""),
    }

    _MANAGED_TRADES[trade_id] = trade
    _INTENT_HISTORY.append(intent)

    publish_event({
        "type": "INTENT_ACCEPTED",
        "ts": time.time(),
        "account_id": aid,
        "symbol": intent["symbol"],
        "intent_id": intent["intent_id"],
        "trade_id": trade_id,
        "source": intent.get("source"),
        "strategy_id": intent.get("strategy_id"),
        "trigger": intent.get("trigger"),
    })
    publish_event({
        "type": "TRADE_CREATED",
        "ts": time.time(),
        "trade_id": trade_id,
        "account_id": aid,
        "symbol": intent["symbol"],
        "state": "PLANNED",
    })

    return {"ok": True, "trade": trade}


@app.post("/v1/trades/place")
async def api_place_managed_trade(request: Request):
    """Place a managed trade (entry order + internal bracket rules).

    BrokerView uses this for "manual buy + bracket".
    v1 scope:
      - long-only BUY entries
      - bracket: stop_loss_cents + take_profit_cents (resolved after fill)
    """
    try:
        data = await request.json()
    except Exception:
        data = {}

    # Enforce v1: buy only
    side = str(data.get("side", "buy")).lower().strip() or "buy"
    if side != "buy":
        raise HTTPException(status_code=400, detail="v1 managed trades support buy-only")

    # Place entry order through existing endpoint logic.
    # We call the adapter directly to avoid http self-calls.
    aid = data.get("account_id")
    if not aid:
        if len(pm.adapters) == 1:
            aid = next(iter(pm.adapters.keys()))
        else:
            raise HTTPException(status_code=400, detail="account_id required")
    if aid not in pm.adapters:
        raise HTTPException(status_code=404, detail="unknown account_id")

    snap = pm.get_snapshots().get(aid)
    _guard_manual_order({**data, "allow_short": False}, snap)

    adapter = pm.adapters.get(aid)
    if adapter is None:
        raise HTTPException(status_code=404, detail="adapter missing")

    args = {
        "symbol": data.get("symbol"),
        "side": "buy",
        "qty": data.get("qty"),
        "type": data.get("type", "limit" if bool(data.get("extended_hours", False)) else "market"),
        "time_in_force": data.get("time_in_force", data.get("tif", "day")),
        "limit_price": data.get("limit_price"),
        "stop_price": data.get("stop_price"),
        "trail": data.get("trail"),
        "extended_hours": bool(data.get("extended_hours", False)),
        "note": data.get("note") or "managed_entry",
    }

    # Create trade record first so we have an ID for note tagging
    tid = _new_trade_id()
    t: Dict[str, Any] = {
        "trade_id": tid,
        "created_ts": time.time(),
        "account_id": aid,
        "symbol": str(args.get("symbol") or "").upper().strip(),
        "side": "buy",
        "qty": float(args.get("qty") or 0),
        "state": "ENTERING",
        "extended_hours": bool(args.get("extended_hours", False)),
        "entry_order_id": None,
        "entry_avg_price": None,
        # bracket config
        "bracket": "PL_SIMPLE",
        "stop_loss_cents": _as_float(data.get("stop_loss_cents")),
        "take_profit_cents": _as_float(data.get("take_profit_cents")),
        "stop_price": _as_float(data.get("stop_price")),
        "take_profit_price": _as_float(data.get("take_profit_price")),
        # exit
        "exit_order_id": None,
        "exit_reason": None,
        "error": None,
    }

    # Tag note with trade_id for traceability
    args["note"] = f"{args.get('note')};trade_id={tid}"

    try:
        res = adapter.place_order(**args)
    except Exception as e:
        t["state"] = "ERROR"
        t["error"] = str(e)
        _MANAGED_TRADES[tid] = t
        raise HTTPException(status_code=500, detail=str(e))

    t["entry_order_id"] = res.get("id") or res.get("client_order_id") or res.get("order_id")
    _MANAGED_TRADES[tid] = t

    return {"ok": True, "trade": t, "entry_order": res}
