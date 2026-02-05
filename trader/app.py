# trader/app.py


import os
import time
import asyncio
import logging
import threading
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

from typing import Dict, Any, Optional, List, Deque
from collections import deque as _deque
from uuid import uuid4
from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from .portfolio_manager import PortfolioManager
from .alpaca_trade_updates import listen_trade_updates
from .trade_runner import TradeRunner

from datetime import timezone as _tz

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
            
            if asyncio.iscoroutinefunction(fn):
                await fn()
            else:
                await asyncio.to_thread(fn)
    except Exception:
        log.exception("startup.register_from_db.failed")

    # 2) Env fallback (restores the "worked yesterday" behavior when DB is empty)
    try:
        if not getattr(pm, "adapters", {}):
            # Alpaca
            if _env_present("ALPACA_API_KEY_ID", "ALPACA_API_KEY") and _env_present("ALPACA_API_SECRET_KEY", "ALPACA_API_SECRET"):
                from .adapters.alpaca_adapter import AlpacaAdapter

                key_id = (os.getenv("ALPACA_API_KEY_ID") or os.getenv("ALPACA_API_KEY") or "").strip()
                secret = (os.getenv("ALPACA_API_SECRET_KEY") or os.getenv("ALPACA_API_SECRET") or "").strip()
                base = (os.getenv("ALPACA_BASE_URL") or "https://paper-api.alpaca.markets").strip()
                # Keep the canonical account_id BrokerView already uses
                aid = (os.getenv("TRADER_DEFAULT_ACCOUNT_ID") or "alpaca:paper").strip()
                pm.adapters[aid] = AlpacaAdapter(account_id=aid, base=base, key_id=key_id, secret=secret)

            # SIM fallback (useful even without broker creds)
            if not getattr(pm, "adapters", {}):
                from .portfolio_manager import SimAdapter

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

# ---------------------------------------------------------------------------
# Broker-truth Trade Ledger (in-memory)
#
# Purpose: keep Trader's books consistent with broker reality, even when
# trades are initiated/closed manually (BrokerView buttons, broker UI, etc.).
#
# Key rule: if the broker is flat for a symbol/account, Trader must treat any
# corresponding open trade as CLOSED on our books.
# ---------------------------------------------------------------------------

# (account_id, symbol) -> open ledger trade
_LEDGER_OPEN: Dict[str, Dict[str, Any]] = {}

# Closed ledger trades (newest first)
_LEDGER_CLOSED: Deque[Dict[str, Any]] = _deque(maxlen=int(os.getenv("TRADER_LEDGER_CLOSED_MAX", "5000") or 5000))

# Last seen qty/avg per (account_id, symbol)
_LEDGER_LAST_POS: Dict[str, Dict[str, float]] = {}


def _ls_key(aid: str, symbol: str) -> str:
    return f"{aid}::{symbol.upper()}"


def _epoch_to_iso(x: Any) -> Optional[str]:
    try:
        v = float(x)
        return datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
    except Exception:
        return None


def _pick_fill_price_and_ts(o: Dict[str, Any]) -> tuple[Optional[float], Optional[str]]:
    """Best-effort extraction of fill price + ts from a local/broker order doc."""
    try:
        raw = o.get("raw") if isinstance(o.get("raw"), dict) else {}

        # Price can live either in the embedded raw (broker adapter) or at the top
        # level (trade_updates telemetry).
        px = raw.get("avg_fill_price")
        if px is None:
            px = raw.get("first_fill_price")
        if px is None:
            px = o.get("avg_fill_price")
        if px is None:
            px = o.get("first_fill_price")
        if px is None:
            px = o.get("filled_avg_price")
        px_f = float(px) if px is not None else None

        # Timestamps can also live in both places.
        ts = raw.get("filled_ts") or raw.get("first_fill_ts")
        if ts is None:
            ts = o.get("filled_ts") or o.get("first_fill_ts")
        iso = _epoch_to_iso(ts) if ts is not None else None
        if iso is None:
            # fall back to ISO fields
            iso = raw.get("updated_at") or o.get("updated_at") or o.get("submitted_at")
        return px_f, (str(iso) if iso else None)
    except Exception:
        return None, None


def _latest_filled_order(account_id: str, symbol: str, side: str) -> Optional[Dict[str, Any]]:
    """Return the most recent filled order for account+symbol+side from local cache."""
    try:
        sym = (symbol or "").upper()
        want_side = (side or "").lower().strip()
        best = None
        best_ts = 0.0
        for o in list(_LOCAL_ORDERS.values()):
            try:
                if o.get("account_id") != account_id:
                    continue
                if (o.get("symbol") or "").upper() != sym:
                    continue
                if (o.get("status") or "").lower() != "filled":
                    continue
                if (o.get("side") or "").lower() != want_side:
                    continue
                raw = o.get("raw") if isinstance(o.get("raw"), dict) else {}
                ts = raw.get("filled_ts") or raw.get("first_fill_ts")
                if ts is None:
                    # ISO fallback
                    iso = raw.get("updated_at") or o.get("updated_at")
                    if iso:
                        try:
                            ts = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
                        except Exception:
                            ts = None
                tsv = float(ts) if ts is not None else 0.0
                if tsv >= best_ts:
                    best_ts = tsv
                    best = o
            except Exception:
                continue
        return best
    except Exception:
        return None


async def _ledger_sync_account(account_id: str) -> None:
    """Sync the broker-truth ledger for one account from PortfolioManager snapshot."""
    try:
        snap = getattr(pm, "portfolio", {}).get(account_id)
        if not snap:
            return
        # snapshot dict form
        sdict = snap.to_dict() if hasattr(snap, "to_dict") else (snap if isinstance(snap, dict) else {})
        positions = sdict.get("positions") or []

        current: Dict[str, Dict[str, float]] = {}
        for p in positions:
            try:
                sym = str(p.get("symbol") if isinstance(p, dict) else getattr(p, "symbol", "")).upper()
                if not sym:
                    continue
                qty = float(p.get("qty") if isinstance(p, dict) else getattr(p, "qty", 0.0) or 0.0)
                avg = float(p.get("avg_price") if isinstance(p, dict) else getattr(p, "avg_price", 0.0) or 0.0)
                if abs(qty) < 1e-12:
                    qty = 0.0
                current[sym] = {"qty": qty, "avg": avg}
            except Exception:
                continue

        prev = _LEDGER_LAST_POS.get(account_id, {})
        all_syms = set(prev.keys()) | set(current.keys())

        updated_iso = sdict.get("updated_at") or _iso_now()

        for sym in all_syms:
            prev_qty = float(prev.get(sym, {}).get("qty", 0.0) or 0.0)
            prev_avg = float(prev.get(sym, {}).get("avg", 0.0) or 0.0)
            cur_qty = float(current.get(sym, {}).get("qty", 0.0) or 0.0)
            cur_avg = float(current.get(sym, {}).get("avg", 0.0) or 0.0)

            k = _ls_key(account_id, sym)

            # OPEN: 0 -> nonzero
            if abs(prev_qty) < 1e-12 and abs(cur_qty) >= 1e-12:
                if k not in _LEDGER_OPEN:
                    side = "LONG" if cur_qty >= 0 else "SHORT"
                    _LEDGER_OPEN[k] = {
                        "trade_id": str(uuid4()),
                        "account_id": account_id,
                        "symbol": sym,
                        "side": side,
                        "qty": abs(cur_qty),
                        "entry_price": cur_avg,
                        "entry_ts": updated_iso,
                        "open_source": "broker_reconcile",
                    }
                    log.info("ledger.open", extra={"account_id": account_id, "symbol": sym, "qty": cur_qty})

            # CLOSE: nonzero -> 0
            if abs(prev_qty) >= 1e-12 and abs(cur_qty) < 1e-12:
                ot = _LEDGER_OPEN.pop(k, None)
                if ot is None:
                    # synthesize an open trade record (best-effort)
                    side = "LONG" if prev_qty >= 0 else "SHORT"
                    ot = {
                        "trade_id": str(uuid4()),
                        "account_id": account_id,
                        "symbol": sym,
                        "side": side,
                        "qty": abs(prev_qty),
                        "entry_price": prev_avg,
                        "entry_ts": updated_iso,
                        "open_source": "broker_reconcile_synth",
                    }

                # try to pick exit price/ts from local filled orders
                exit_side = "sell" if str(ot.get("side") or "LONG").upper() == "LONG" else "buy"
                last_fill = _latest_filled_order(account_id, sym, exit_side)
                exit_px, exit_ts = _pick_fill_price_and_ts(last_fill or {})

                doc = {
                    "trade_id": ot.get("trade_id"),
                    "account_id": account_id,
                    "symbol": sym,
                    "side": ot.get("side") or "LONG",
                    "qty": ot.get("qty"),
                    "entry_price": ot.get("entry_price"),
                    "entry_ts": ot.get("entry_ts"),
                    "exit_price": exit_px,
                    "exit_ts": exit_ts or updated_iso,
                    "pnl": None,
                    "close_reason": "BROKER_FLAT",
                    "close_source": "broker_reconcile",
                }

                try:
                    if doc["entry_price"] is not None and doc["exit_price"] is not None and doc["qty"] is not None:
                        doc["pnl"] = (float(doc["exit_price"]) - float(doc["entry_price"])) * float(doc["qty"]) \
                            * (1.0 if str(doc.get("side") or "LONG").upper() == "LONG" else -1.0)
                except Exception:
                    pass

                _LEDGER_CLOSED.appendleft(doc)
                log.info("ledger.close", extra={"account_id": account_id, "symbol": sym, "pnl": doc.get("pnl")})

        _LEDGER_LAST_POS[account_id] = current
    except Exception:
        log.exception("ledger.sync.failed", extra={"account_id": account_id})

# ---------------------------------------------------------------------------
# Intent -> Trade (missing machinery)
# ---------------------------------------------------------------------------

def _norm_intent_payload(payload: Any) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Accept either {intent:{...}, meta:{...}} or a flat dict."""
    if isinstance(payload, dict) and isinstance(payload.get("intent"), dict):
        intent = payload.get("intent") or {}
        meta = payload.get("meta") or {}
        return dict(intent), dict(meta) if isinstance(meta, dict) else {}
    if isinstance(payload, dict):
        return dict(payload), {}
    return {}, {}

def _find_existing_trade(aid: str, symbol: str) -> Optional[Dict[str, Any]]:
    try:
        for t in _MANAGED_TRADES.values():
            if not isinstance(t, dict):
                continue
            if str(t.get("account_id") or "") != aid:
                continue
            if str(t.get("symbol") or "").upper() != symbol.upper():
                continue
            st = str(t.get("state") or "").upper()
            if st not in ("DONE", "ERROR"):
                return t
    except Exception:
        pass
    return None

def _default_bot_qty() -> float:
    # Keep this boring and env-driven.
    # You can later replace with Risk/Capital sizing.
    try:
        return float(os.getenv("TRADER_DEFAULT_QTY") or os.getenv("TRADER_BOT_QTY") or "1")
    except Exception:
        return 1.0


# ---------------------------------------------------------------------------
# Intent v3 normalization (reflex.intent.v3)
# ---------------------------------------------------------------------------

def _coerce_model(x: Any, *, default_kind: str) -> Dict[str, Any]:
    """Return {kind, params} from either a string or a dict.

    Legacy emitters may send:
      - "tslfe"                (string)
      - {"kind": "tslfe", ...} (dict)
      - {"model": "tslfe"}    (dict)
    """
    if isinstance(x, str):
        k = x.strip() or default_kind
        return {"kind": k, "params": {}}
    if isinstance(x, dict):
        k = (x.get("kind") or x.get("model") or default_kind)
        params = x.get("params") if isinstance(x.get("params"), dict) else {k2: v2 for k2, v2 in x.items() if k2 not in ("kind", "model")}
        return {"kind": str(k), "params": dict(params) if isinstance(params, dict) else {}}
    return {"kind": default_kind, "params": {}}


def _default_models() -> Dict[str, Any]:
    return {
        "entry": {"kind": "immediate", "params": {}},
        "position_mgmt": {"kind": "none", "params": {}},
        "profit": {"kind": "tslfe", "params": {}},
        "stop": {"kind": "5pt_hard", "params": {"cents": 5}},
        "exit": {"kind": "tslfe", "params": {}},
    }


def _synthesize_models(intent: Dict[str, Any]) -> Dict[str, Any]:
    """Prefer intent.models; else build from legacy fields + defaults."""
    d = _default_models()
    models = intent.get("models")
    if isinstance(models, dict):
        out: Dict[str, Any] = {}
        for k in ("entry", "position_mgmt", "profit", "stop", "exit"):
            out[k] = _coerce_model(models.get(k), default_kind=d[k]["kind"])
            # merge defaults for stop cents if missing
            if k == "stop" and not out[k].get("params"):
                out[k]["params"] = dict(d[k]["params"])
        return out

    # Legacy fields
    out = {
        "entry": _coerce_model(intent.get("entry_model"), default_kind=d["entry"]["kind"]),
        "position_mgmt": _coerce_model(intent.get("position_management_model"), default_kind=d["position_mgmt"]["kind"]),
        "profit": _coerce_model(intent.get("profit_model"), default_kind=d["profit"]["kind"]),
        "stop": _coerce_model(intent.get("stop_model") or intent.get("stop_loss_model"), default_kind=d["stop"]["kind"]),
        "exit": _coerce_model(intent.get("exit_model"), default_kind=d["exit"]["kind"]),
    }
    # Apply defaults for missing stop params
    if not isinstance(out["stop"].get("params"), dict) or not out["stop"]["params"]:
        out["stop"]["params"] = dict(d["stop"]["params"])
    return out


def _parse_iso_ts_to_epoch(ts: str) -> Optional[float]:
    try:
        s = (ts or "").strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        return dt.timestamp()
    except Exception:
        return None


def _normalize_trigger(intent: Dict[str, Any]) -> Dict[str, Any]:
    """Unified trigger object for LIVE + REPLAY.

    Defaults to kind=now.
    """
    trig = intent.get("trigger")
    if not isinstance(trig, dict):
        return {"kind": "now", "session": "ANY", "params": {}}

    kind = str(trig.get("kind") or "now").strip().lower()
    session = str(trig.get("session") or "ANY").strip().upper() or "ANY"
    params = trig.get("params") if isinstance(trig.get("params"), dict) else {}

    if kind == "at_time":
        ts_iso = trig.get("ts")
        ts_epoch = _parse_iso_ts_to_epoch(str(ts_iso or "")) if ts_iso is not None else None
        p = dict(params)
        tol_ms = int(p.get("tolerance_ms") or 500)
        late_action = str(p.get("late_action") or "execute").strip().lower()
        if late_action not in ("execute", "skip", "expire"):
            late_action = "execute"
        p["tolerance_ms"] = tol_ms
        p["late_action"] = late_action
        return {"kind": "at_time", "ts": ts_iso, "ts_epoch": ts_epoch, "session": session, "params": p}

    return {"kind": "now", "session": session, "params": dict(params)}

@app.post("/v1/intents")
async def api_intents_ingest(request: Request):
    """Standard execution front-door: turn an intent into a managed trade.

    This is what broker_worker expects to call.
    TradeRunner will place the entry order asynchronously.
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    intent, meta = _norm_intent_payload(payload)

    symbol = str(intent.get("symbol") or "").strip().upper()
    side = str(intent.get("side") or "").strip().lower()
    if side in ("short", "sell_short", "sellshort"):
        side = "sell"
    account_id = (intent.get("account_id") or intent.get("account") or "").strip() or None
    if not account_id:
        account_id = _default_account_id()
    if not account_id:
        return JSONResponse({"ok": False, "error": "account_id required (multiple accounts present)"}, status_code=400)

    if account_id not in pm.adapters:
        return JSONResponse({"ok": False, "error": f"unknown account_id: {account_id}"}, status_code=404)

    if not symbol or side not in ("buy", "sell"):
        return JSONResponse({"ok": False, "error": "intent requires symbol and side (buy|sell)"}, status_code=400)

    # prevent duplicate active trades per (account,symbol)
    existing = _find_existing_trade(account_id, symbol)
    if existing is not None:
        return {"ok": True, "status": "ignored_duplicate", "trade_id": existing.get("trade_id"), "symbol": symbol, "account_id": account_id}

    qty = intent.get("qty") or intent.get("shares") or intent.get("quantity")
    try:
        qty_f = float(qty) if qty not in (None, "", 0) else _default_bot_qty()
    except Exception:
        qty_f = _default_bot_qty()
    if qty_f <= 0:
        qty_f = _default_bot_qty()

    trade_id = str(intent.get("trade_id") or intent.get("intent_id") or uuid4())
    sess = str(intent.get("market_session") or os.getenv("MARKET_SESSION") or "RTH").upper()
    if sess not in ("PRE", "RTH", "POST"):
        sess = "RTH"

    # --- Canonical v3 fields (best-effort; keep legacy compatible) ---------
    schema = str(intent.get("schema") or "").strip() or None
    mode = str(intent.get("mode") or os.getenv("REFLEX_MODE") or "LIVE").strip().upper()
    if mode not in ("LIVE", "REPLAY"):
        mode = "LIVE"

    gen_id = str(intent.get("gen_id") or "").strip()
    if len(gen_id) != 1:
        # Keep non-blocking: derive a 1-char provenance from known fields
        src = str(intent.get("source") or intent.get("strategy_id") or "?").strip()
        gen_id = (src[:1] or "?")

    models = _synthesize_models(intent)
    trigger = _normalize_trigger(intent)

    # "always protected" invariant: ensure a stop model exists
    try:
        sk = str(models.get("stop", {}).get("kind") or "").strip()
        if not sk:
            models["stop"] = {"kind": "5pt_hard", "params": {"cents": 5}}
    except Exception:
        models["stop"] = {"kind": "5pt_hard", "params": {"cents": 5}}

    trade = {
        "trade_id": trade_id,
        "account_id": account_id,
        "symbol": symbol,
        "side": side,
        "qty": qty_f,
        "state": "ENTRY_PENDING",
        "created_ts": time.time(),
        "market_session": sess,
        "strategy_id": intent.get("strategy_id") or intent.get("strategy"),
        "intent_strength": intent.get("strength"),
        "intent": intent,
        "meta": meta,

        # Canonical intent v3 fields
        "intent_schema": schema,
        "intent_mode": mode,
        "gen_id": gen_id,
        "trigger": trigger,
        "models": models,

        # Keep legacy model fields populated for older paths/loggers
        "entry_model": (models.get("entry") or {}).get("kind"),
        "position_mgmt_model": (models.get("position_mgmt") or {}).get("kind"),
        "profit_model": (models.get("profit") or {}).get("kind"),
        "stop_loss_model": (models.get("stop") or {}).get("kind"),
        "exit_model": (models.get("exit") or {}).get("kind"),
    }

    _MANAGED_TRADES[trade_id] = trade

    # keep short history for debugging
    try:
        _INTENT_HISTORY.append(
            {
                "ts": _iso_now(),
                "trade_id": trade_id,
                "account_id": account_id,
                "symbol": symbol,
                "side": side,
                "qty": qty_f,
                "strategy_id": trade.get("strategy_id"),
                "strength": trade.get("intent_strength"),
            }
        )
    except Exception:
        pass

    return {"ok": True, "status": "accepted", "trade_id": trade_id, "account_id": account_id, "symbol": symbol, "side": side, "qty": qty_f}

@app.get("/v1/intents/recent")
async def api_intents_recent(limit: int = Query(50, ge=1, le=500)):
    try:
        items = list(_INTENT_HISTORY)[-int(limit):]
        items.reverse()
        return {"items": items}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/v1/trades")
async def api_trades(account_id: Optional[str] = Query(None), state: Optional[str] = Query(None), limit: int = Query(500, ge=1, le=2000)):
    try:
        out = []
        for t in list(_MANAGED_TRADES.values()):
            if account_id and t.get("account_id") != account_id:
                continue
            if state and str(t.get("state") or "").upper() != str(state).upper():
                continue
            out.append(dict(t))
        out.sort(key=lambda x: float(x.get("created_ts") or 0.0), reverse=True)
        return {"items": out[: int(limit)]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
        """
        Return MD dict for TradeRunner: {"lt":..., "bid":..., "ask":...}

        Priority:
        1) PortfolioManager.get_md(symbol) if it exists (preferred)
        2) md_worker Redis cache: key = TRADER_MD_KEY_PREFIX + ":" + SYMBOL
            default prefix = reflex:{instance_id}:md
        """
        sym = str(symbol or "").upper().strip()
        if not sym:
            return {}

        # 1) If pm has a live get_md implementation, use it.
        try:
            fn = getattr(pm, "get_md", None)
            if fn is not None:
                out = fn(sym) or {}
                if isinstance(out, dict) and (out.get("lt") is not None or out.get("bid") is not None or out.get("ask") is not None):
                    return out
        except Exception:
            pass

        # 2) Fallback: read md_worker Redis key directly.
        try:
            import json as _json
            import redis  # type: ignore

            instance_id = (os.getenv("REFLEX_INSTANCE_ID") or os.getenv("INSTANCE") or "live").strip()
            tpl = (os.getenv("TRADER_MD_KEY_PREFIX", "reflex:{instance_id}:md") or "reflex:{instance_id}:md").strip()
            prefix = tpl.format(instance_id=instance_id).rstrip(":")
            key = f"{prefix}:{sym}"

            r = redis.Redis.from_url(get_redis_url(), decode_responses=True)
            raw = r.get(key)
            if not raw:
                return {}

            doc = _json.loads(raw)
            # md_worker writes dataclass-as-dict: {"symbol","last_trade","last_quote","updated_ts"}
            lt = None
            bid = None
            ask = None

            try:
                lt = doc.get("last_trade", {}).get("price")
            except Exception:
                lt = None
            try:
                bid = doc.get("last_quote", {}).get("bid")
                ask = doc.get("last_quote", {}).get("ask")
            except Exception:
                bid = None
                ask = None

            out = {}
            if lt is not None:
                out["lt"] = float(lt)
            if bid is not None:
                out["bid"] = float(bid)
            if ask is not None:
                out["ask"] = float(ask)

            # helpful freshness (optional)
            if doc.get("updated_ts") is not None:
                out["updated_ts"] = doc.get("updated_ts")

            return out
        except Exception:
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
                # Keep internal ledger in sync with broker truth.
                await _ledger_sync_account(aid)
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
            key_id = getattr(a, "key_id", None) or os.getenv("ALPACA_API_KEY_ID") or os.getenv("ALPACA_API_KEY") or ""
            secret = getattr(a, "secret", None) or os.getenv("ALPACA_API_SECRET_KEY") or os.getenv("ALPACA_API_SECRET") or ""
            if not key_id or not secret:
                continue

            def _on_evt_factory(account_id: str):
                def _on_evt(evt: Dict[str, Any]) -> None:
                    _apply_trade_update_telemetry(account_id, evt)
                    try:
                        # record event for UI/debug if supported
                        rec = getattr(pm, "record_broker_event", None)
                        if rec is not None:
                            loop.call_soon_threadsafe(
                                asyncio.create_task,
                                _call_maybe_async(rec, account_id, evt),
                            )

                        # immediate reconcile on broker updates
                        rec_acc = getattr(pm, "reconcile_account", None)
                        if rec_acc is not None:
                            loop.call_soon_threadsafe(
                                asyncio.create_task,
                                _call_maybe_async(rec_acc, account_id),
                            )

                        # ledger sync (best-effort): once reconcile completes,
                        # pull snapshot and detect open/close transitions.
                        loop.call_soon_threadsafe(
                            asyncio.create_task,
                            _ledger_sync_account(account_id),
                        )

                        # refresh orders cache so statuses advance (SUBMITTED->FILLED)
                        loop.call_soon_threadsafe(
                            asyncio.create_task,
                            _refresh_orders_cache_once(),
                        )
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
@app.get("/v1/debug/routes")
async def debug_routes():
    """List registered routes so we can confirm the running process has our debug endpoints."""
    out = []
    try:
        for r in app.routes:
            methods = sorted(list(getattr(r, "methods", []) or []))
            path = getattr(r, "path", None)
            name = getattr(r, "name", None)
            if path:
                out.append({"path": path, "methods": methods, "name": name})
    except Exception:
        pass
    out.sort(key=lambda x: x["path"])
    return {"items": out}
@app.get("/v1/debug/md")
async def debug_md(symbol: str = Query(...)):
    """
    Show exactly what key Trader reads for md_worker cache and the raw JSON stored there.
    This isolates: redis_url / instance_id / key_prefix mismatches.
    """
    sym = str(symbol or "").upper().strip()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")

    # The exact same prefix logic used in your get_md() fallback
    instance_id = (os.getenv("REFLEX_INSTANCE_ID") or os.getenv("INSTANCE") or "live").strip()
    tpl = (os.getenv("TRADER_MD_KEY_PREFIX", "reflex:{instance_id}:md") or "reflex:{instance_id}:md").strip()
    prefix = tpl.format(instance_id=instance_id).rstrip(":")
    key = f"{prefix}:{sym}"

    redis_url = (os.getenv("REDIS_URL") or os.getenv("REFLEX_REDIS_URL") or "redis://127.0.0.1:6379/0").strip()

    raw = None
    err = None
    doc = None
    ttl = None
    try:
        import redis  # type: ignore
        r = redis.Redis.from_url(redis_url, decode_responses=True)
        raw = r.get(key)
        ttl = r.ttl(key)
        if raw:
            import json as _json
            doc = _json.loads(raw)
    except Exception as e:
        err = str(e)

    # Also show what store.get_md returns right now
    store = _StoreShim()
    md = store.get_md(sym) or {}

    return {
        "symbol": sym,
        "redis_url": redis_url,
        "instance_id": instance_id,
        "key_prefix": prefix,
        "key": key,
        "ttl": ttl,
        "raw": raw,
        "doc": doc,
        "store_get_md": md,
        "error": err,
    }

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
                "gen_id": t.get("gen_id"),
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
@app.on_event("startup")
async def on_startup():
    global _MAIN_LOOP, _bg_reconcile_task, _bg_orders_task
    log.info("startup.begin")
    loop = asyncio.get_event_loop()
    _MAIN_LOOP = loop

    # IMPORTANT: register adapters before starting background loops.
    # If this is skipped, BrokerView has no accounts and polling yields nonsense.
    await _register_adapters_once()

    # Prime an initial snapshot so UI isn't blank for the first poll cycle.
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
        return {"items": items}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@app.get("/events")
async def events_sse(request: Request):
    """
    Server-Sent Events stream for BrokerView (and any other UI).

    BrokerView has historically polled /events; some builds only exposed /v1/events.
    This endpoint keeps the UI happy and avoids console spam.

    Contract:
      - Never blocks the event loop
      - Never throws (always yields heartbeats / status)
    """
    import json as _json

    async def _gen():
        hello = {"type": "status", "status": "connected", "ts": time.time(), "iso_utc": _iso_now()}
        yield f"event: status\ndata: {_json.dumps(hello)}\n\n".encode("utf-8")

        while True:
            if await request.is_disconnected():
                break
            try:
                payload = {
                    "type": "heartbeat",
                    "ts": time.time(),
                    "iso_utc": _iso_now(),
                    "accounts": len(getattr(pm, "adapters", {}) or {}),
                }

                # best-effort: include a small tail of recent events
                try:
                    items = []
                    pm_events = getattr(pm, "_events", {}) or {}
                    if isinstance(pm_events, dict):
                        for aid, dq in pm_events.items():
                            for ev in (dq or []):
                                if isinstance(ev, dict):
                                    items.append({"account_id": aid, **ev})
                    for ev in list(_INTENT_HISTORY)[-25:]:
                        if isinstance(ev, dict):
                            items.append(ev)
                    if items:
                        payload["items"] = items[-50:]
                except Exception:
                    pass

                yield f"event: events\ndata: {_json.dumps(payload)}\n\n".encode("utf-8")
            except Exception:
                err = {"type": "status", "status": "error", "ts": time.time(), "iso_utc": _iso_now()}
                yield f"event: status\ndata: {_json.dumps(err)}\n\n".encode("utf-8")
            await asyncio.sleep(1.0)

    return StreamingResponse(_gen(), media_type="text/event-stream")

@app.get("/v1/accounts")
async def accounts():
    """
    BrokerView expects this. Return lightweight overview for all accounts.
    """
    try:
        state = pm.get_state() or {}
        accs = state.get("accounts") or {}

        # PortfolioManager returns a dict keyed by account_id. BrokerView expects
        # a list of account objects.
        if isinstance(accs, dict):
            items = list(accs.values())
        elif isinstance(accs, list):
            items = accs
        else:
            items = []

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

    Cache-only, but we enrich positions with Trader-managed "virtual" stop/target
    so the Positions box can display soft protection even when no broker orders exist.
    """
    try:
        snaps = pm.get_snapshots()
        snap = snaps.get(account_id) or {}
        positions = snap.get("positions") or []

        # Build quick lookup of active managed trades by symbol for this account
        active_by_sym: Dict[str, Dict[str, Any]] = {}
        try:
            for t in list(_MANAGED_TRADES.values()):
                if not isinstance(t, dict):
                    continue
                if str(t.get("account_id") or "") != str(account_id):
                    continue
                st = str(t.get("state") or "").upper()
                if st in ("DONE", "ERROR"):
                    continue
                sym = str(t.get("symbol") or "").upper()
                if sym:
                    active_by_sym[sym] = t
        except Exception:
            active_by_sym = {}

        out: List[Dict[str, Any]] = []
        for p in positions:
            if not isinstance(p, dict):
                continue

            d = dict(p)
            sym = str(d.get("symbol") or "").upper()
            t = active_by_sym.get(sym)

            if isinstance(t, dict):
                # "Virtual orders" for UI
                d["trade_id"] = t.get("trade_id")
                d["trade_state"] = t.get("state")

                # PTI provenance tag (single letter)
                d["gen_id"] = t.get("gen_id")

                # Prefer explicit compat fields, fall back to soft fields
                stop_px = t.get("stop_price") or t.get("soft_stop_price")
                tp_px = t.get("take_profit_price") or t.get("soft_target_price")

                # Common field names UIs might look for
                d["stop_price"] = stop_px
                d["take_profit_price"] = tp_px
                d["target_price"] = tp_px

                # Keep originals too (handy for debug)
                d["soft_stop_price"] = t.get("soft_stop_price")
                d["soft_target_price"] = t.get("soft_target_price")

                # Helpful for display
                if t.get("entry_avg_price") is not None:
                    d["avg_price"] = d.get("avg_price") or t.get("entry_avg_price")
                    d["avg_entry_price"] = d.get("avg_entry_price") or t.get("entry_avg_price")

            out.append(d)

        return {"items": out, "account_id": account_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@app.get("/v1/portfolio/closed_positions")
async def portfolio_closed_positions(
    account_id: Optional[str] = Query(None),
    scope: str = Query("today"),  # today|all
    limit: int = Query(500, ge=1, le=5000),
):
    """Closed positions as computed/persisted by Trader.

    This is the canonical, truth-first view for BrokerView. UI must not infer.
    """
    try:
        scope_l = (scope or "today").lower().strip()
        tz = None
        try:
            if ZoneInfo is not None:
                tz = ZoneInfo("America/New_York")
        except Exception:
            tz = None

        today_et = None
        if scope_l == "today" and tz is not None:
            today_et = datetime.now(tz=tz).date()

        items: List[Dict[str, Any]] = []

        # 1) Broker-truth ledger closes (covers manual buy/sell and broker-side closes)
        for t in list(_LEDGER_CLOSED):
            try:
                if not isinstance(t, dict):
                    continue
                if account_id and str(t.get("account_id") or "") != str(account_id):
                    continue

                exit_ts = t.get("exit_ts")
                if today_et is not None and exit_ts:
                    try:
                        d = datetime.fromisoformat(str(exit_ts).replace("Z", "+00:00"))
                        if tz is not None:
                            d = d.astimezone(tz)
                        if d.date() != today_et:
                            continue
                    except Exception:
                        pass

                items.append({
                    "trade_id": t.get("trade_id"),
                    "account_id": t.get("account_id"),
                    "gen_id": t.get("gen_id"),
                    "symbol": t.get("symbol"),
                    "side": t.get("side") or "LONG",
                    "qty": t.get("qty"),
                    "entry_price": t.get("entry_price"),
                    "exit_price": t.get("exit_price"),
                    "exit_ts": exit_ts,
                    "pnl": t.get("pnl"),
                    "close_reason": t.get("close_reason") or "CLOSED",
                    "close_source": t.get("close_source") or "ledger",
                })
            except Exception:
                continue

        # 2) Managed DONE trades (legacy path)
        for t in list(_MANAGED_TRADES.values()):
            if not isinstance(t, dict):
                continue
            if account_id and str(t.get("account_id") or "") != str(account_id):
                continue
            if str(t.get("state") or "").upper() != "DONE":
                continue

            exit_ts = t.get("exit_filled_ts") or t.get("exit_ts") or t.get("closed_ts") or t.get("updated_iso") or None
            # Today filter (ET)
            if today_et is not None and exit_ts:
                try:
                    d = datetime.fromisoformat(str(exit_ts).replace("Z", "+00:00"))
                    if tz is not None:
                        d = d.astimezone(tz)
                    if d.date() != today_et:
                        continue
                except Exception:
                    pass

            qty = t.get("qty")
            entry_px = t.get("entry_avg_price")
            exit_px = t.get("exit_fill_price") or t.get("exit_price")

            doc = {
                "trade_id": t.get("trade_id"),
                "account_id": t.get("account_id"),
                "gen_id": t.get("gen_id"),
                "symbol": t.get("symbol"),
                "side": t.get("side") or "LONG",
                "qty": qty,
                "entry_price": entry_px,
                "exit_price": exit_px,
                "exit_ts": exit_ts,
                "pnl": t.get("realized_pl"),
                "close_reason": t.get("close_reason") or t.get("exit_reason") or "CLOSED",
            }

            # Backfill realized_pl if missing but we have prices
            try:
                if doc["pnl"] is None and entry_px is not None and exit_px is not None and qty is not None:
                    q = float(qty)
                    ep = float(entry_px)
                    xp = float(exit_px)
                    if q and ep and xp:
                        doc["pnl"] = (xp - ep) * q
            except Exception:
                pass

            items.append(doc)

        def _k(x: Dict[str, Any]) -> str:
            return str(x.get("exit_ts") or "")

        items.sort(key=_k, reverse=True)
        return {"items": items[: int(limit)], "scope": scope_l, "account_id": account_id}
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

    # Ensure broker and local echo share the same client_order_id for reconciliation
    args["client_order_id"] = client_id

    note = str(args.get("note") or "")
    if "cid=" not in note:
        note = (note + "; " if note else "") + f"cid={client_id}"
    args["note"] = note

    local = {
        "account_id": aid,
        "id": client_id,
        "client_order_id": client_id,
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
        "source": "local",
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