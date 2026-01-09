# trader/trade_runner.py
# v1.1 — Real TradeRunner (fixed + hardened):
#   - Consumes trades with state == "READY"
#   - Places entry order (market)
#   - Detects fill (orders + positions fallback)
#   - Places broker-side protection STOP
#   - Confirms protection exists at broker
#   - If protection can't be confirmed quickly: FLATTEN + ABORT_PROTECTION
#
# Key fixes vs v1.0:
#   - Removed invalid Python "as dict" cast (fatal syntax error)
#   - Handles sync OR async adapter.place_order()
#   - Safer qty/risk extraction

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("trader.trade_runner")


def _truthy_env(name: str, default: str = "0") -> bool:
    v = str(os.getenv(name, default)).strip().lower()
    return v in ("1", "true", "yes", "y", "on")


def _as_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _now() -> float:
    return time.time()


def _client_ids(trade_id: str) -> Tuple[str, str, str]:
    entry_cid = f"{trade_id}:entry"
    stop_cid = f"{trade_id}:stop"
    flatten_cid = f"{trade_id}:flatten"
    return entry_cid, stop_cid, flatten_cid


def _side_kind(side: str) -> str:
    s = (side or "").lower().strip()
    if s in ("buy", "long"):
        return "long"
    if s in ("sell", "short"):
        return "short"
    return "long"


def _opp_side_for_close(kind: str) -> str:
    return "sell" if kind == "long" else "buy"


def _stop_side_for_protection(kind: str) -> str:
    return "sell" if kind == "long" else "buy"


def _compute_stop_price(entry_price: float, kind: str) -> float:
    """
    Compute a hard stop price from env.

    Supported env knobs:
      - TRADER_HARD_STOP_PCT (e.g. "0.5" meaning 0.5%)
      - TRADER_HARD_STOP_CENTS (e.g. "20" meaning $0.20)
    Default: 20 cents.
    """
    pct = _as_float(os.getenv("TRADER_HARD_STOP_PCT"))
    cents = _as_float(os.getenv("TRADER_HARD_STOP_CENTS"))
    if cents is None:
        cents = 20.0

    if pct is not None and pct > 0:
        delta = entry_price * (pct / 100.0)
    else:
        delta = cents / 100.0

    sp = (entry_price - delta) if kind == "long" else (entry_price + delta)
    return max(0.01, float(sp))


def _order_status(o: Dict[str, Any]) -> str:
    return str(o.get("status") or "").lower().strip()


def _order_filled(o: Dict[str, Any], want_qty: float) -> bool:
    st = _order_status(o)
    if st == "filled":
        return True
    fq = _as_float(o.get("filled_qty"))
    if fq is not None and fq >= (want_qty - 1e-9):
        return True
    return False


def _safe_round_price(p: float) -> float:
    return float(round(float(p), 2))


def _find_pos(snapshot: Dict[str, Any], symbol: str) -> Optional[Dict[str, Any]]:
    sym = (symbol or "").upper().strip()
    for p in (snapshot.get("positions") or []):
        if str(p.get("symbol") or "").upper().strip() == sym:
            return p
    return None


async def _maybe_await(fn, *args, **kwargs):
    """Call fn which may be sync or async."""
    if fn is None:
        raise RuntimeError("missing function")
    if asyncio.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    res = fn(*args, **kwargs)
    if asyncio.iscoroutine(res):
        return await res
    return res


class TradeRunner:
    def __init__(
        self,
        *,
        pm: Any,
        trades: Dict[str, Dict[str, Any]],
        journal_append,
        publish_event,
        list_orders_for_account_async,
        snapshot_for_account,
    ) -> None:
        self.pm = pm
        self.trades = trades
        self.journal_append = journal_append
        self.publish_event = publish_event
        self._list_orders_for_account_async = list_orders_for_account_async
        self._snapshot_for_account = snapshot_for_account

        self._q: asyncio.Queue[str] = asyncio.Queue(maxsize=5000)
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

        self.enabled = _truthy_env("TRADER_TRADE_RUNNER_ENABLED", "1")
        self.protect_timeout = _as_float(os.getenv("TRADER_PROTECT_TIMEOUT_SECS")) or 2.0
        self.fill_timeout = _as_float(os.getenv("TRADER_FILL_TIMEOUT_SECS")) or 120.0
        self.loop_sleep = _as_float(os.getenv("TRADER_TRADE_RUNNER_TICK_SECS")) or 0.25

    def start(self) -> None:
        if not self.enabled:
            log.warning("TradeRunner disabled (TRADER_TRADE_RUNNER_ENABLED=0)")
            return
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="trade_runner")
        log.info("TradeRunner started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except Exception:
                pass

    def enqueue(self, trade_id: str) -> None:
        if not self.enabled:
            return
        try:
            self._q.put_nowait(str(trade_id))
        except Exception:
            pass

    async def _run(self) -> None:
        scan_every = 1.0
        last_scan = 0.0

        while not self._stop.is_set():
            try:
                now = _now()
                if (now - last_scan) >= scan_every:
                    last_scan = now
                    for tid, t in list(self.trades.items()):
                        st = str(t.get("state") or "")
                        if st in ("READY", "ENTRY_SUBMITTED", "ENTRY_FILLED", "PROTECT_SUBMITTED"):
                            self.enqueue(tid)

                try:
                    tid = await asyncio.wait_for(self._q.get(), timeout=self.loop_sleep)
                except asyncio.TimeoutError:
                    continue

                await self._advance_one(tid)

            except Exception:
                log.exception("TradeRunner loop crashed; continuing")
                await asyncio.sleep(0.5)

        log.info("TradeRunner stopped")

    def _persist(self, t: Dict[str, Any], event_type: str, extra: Optional[Dict[str, Any]] = None) -> None:
        try:
            self.journal_append("trade", t)
        except Exception:
            pass

        evt = {
            "type": event_type,
            "ts": _now(),
            "account_id": t.get("account_id"),
            "symbol": t.get("symbol"),
            "trade_id": t.get("trade_id"),
            "state": t.get("state"),
        }
        if extra:
            evt.update(extra)
        try:
            self.publish_event(evt)
        except Exception:
            pass

    def _ensure_qty(self, t: Dict[str, Any]) -> float:
        qty = _as_float(t.get("qty"))
        if qty is not None and qty > 0:
            return float(qty)

        shares = None
        risk = t.get("risk")
        if isinstance(risk, dict):
            shares = _as_float(risk.get("shares"))

        if shares is None or shares <= 0:
            shares = 10.0

        t["qty"] = float(shares)
        return float(shares)

    async def _advance_one(self, trade_id: str) -> None:
        t = self.trades.get(trade_id)
        if not t:
            return

        state = str(t.get("state") or "")
        if state in ("DONE", "CANCELED", "ERROR", "ABORT_PROTECTION", "REJECTED"):
            return

        aid = str(t.get("account_id") or "").strip()
        sym = str(t.get("symbol") or "").strip().upper()
        if not aid or not sym:
            t["state"] = "ERROR"
            t["error"] = "missing account_id/symbol"
            self._persist(t, "TRADE_ERROR", extra={"reason": t["error"]})
            return

        adapter = self.pm.adapters.get(aid)
        if adapter is None:
            t["state"] = "ERROR"
            t["error"] = f"missing adapter for {aid}"
            self._persist(t, "TRADE_ERROR", extra={"reason": t["error"]})
            return

        kind = _side_kind(str(t.get("side") or "buy"))
        entry_cid, stop_cid, flatten_cid = _client_ids(trade_id)
        qty = self._ensure_qty(t)

        # 1) READY -> place entry
        if state == "READY":
            trig = t.get("trigger") or {}
            trig_kind = (trig.get("kind") or trig.get("type") or "immediate") if isinstance(trig, dict) else "immediate"
            if str(trig_kind).lower() != "immediate":
                return

            if t.get("entry_order_id"):
                t["state"] = "ENTRY_SUBMITTED"
                self._persist(t, "TRADE_ENTRY_ALREADY_SUBMITTED")
                return

            side_open = "buy" if kind == "long" else "sell"
            payload = {
                "symbol": sym,
                "side": side_open,
                "qty": qty,
                "type": "market",
                "time_in_force": "day",
                "extended_hours": bool(t.get("extended_hours", False)),
                "note": f"intent_entry:trade_id={trade_id}",
                "client_order_id": entry_cid,
            }

            try:
                res = await _maybe_await(getattr(adapter, "place_order", None), **payload)
                oid = res.get("id") or res.get("order_id") or res.get("client_order_id") or entry_cid
                t["entry_order_id"] = str(oid)
                t["entry_client_order_id"] = entry_cid
                t["entry_submitted_ts"] = _now()
                t["state"] = "ENTRY_SUBMITTED"
                self._persist(t, "TRADE_ENTRY_SUBMITTED", extra={"order_id": t["entry_order_id"]})
            except Exception as e:
                t["state"] = "REJECTED"
                t["error"] = f"entry.place_order failed: {e}"
                self._persist(t, "TRADE_ENTRY_REJECTED", extra={"reason": t["error"]})
            return

        # 2) ENTRY_SUBMITTED -> detect fill
        if state == "ENTRY_SUBMITTED":
            start_ts = _as_float(t.get("entry_submitted_ts")) or _now()
            if (_now() - start_ts) > self.fill_timeout:
                t["state"] = "ERROR"
                t["error"] = f"entry fill timeout after {self.fill_timeout}s"
                self._persist(t, "TRADE_ENTRY_TIMEOUT", extra={"reason": t["error"]})
                return

            filled, fill_price = await self._detect_entry_fill(aid, sym, entry_cid, qty)
            if not filled:
                return

            t["entry_filled_ts"] = _now()
            if fill_price and fill_price > 0:
                t["entry_avg_price"] = float(fill_price)
            t["state"] = "ENTRY_FILLED"
            self._persist(t, "TRADE_ENTRY_FILLED", extra={"avg_price": t.get("entry_avg_price")})
            return

        # 3) ENTRY_FILLED -> place protection stop
        if state == "ENTRY_FILLED":
            if t.get("protect_order_id"):
                t["state"] = "PROTECT_SUBMITTED"
                self._persist(t, "TRADE_PROTECT_ALREADY_SUBMITTED")
                return

            entry_price = _as_float(t.get("entry_avg_price")) or 0.0
            if entry_price <= 0:
                snap = self._snapshot_for_account(aid)
                pos = _find_pos(snap, sym)
                if pos:
                    entry_price = _as_float(pos.get("avg_price")) or 0.0
                    if entry_price > 0:
                        t["entry_avg_price"] = entry_price

            if entry_price <= 0:
                return

            stop_price = _as_float(t.get("stop_price"))
            if not stop_price or stop_price <= 0:
                stop_price = _compute_stop_price(entry_price, kind)
                t["stop_price"] = float(stop_price)

            stop_side = _stop_side_for_protection(kind)

            payload = {
                "symbol": sym,
                "side": stop_side,
                "qty": qty,
                "type": "stop",
                "time_in_force": "day",
                "stop_price": _safe_round_price(float(stop_price)),
                "extended_hours": False,
                "note": f"protection_stop:trade_id={trade_id}",
                "client_order_id": stop_cid,
            }

            try:
                res = await _maybe_await(getattr(adapter, "place_order", None), **payload)
                oid = res.get("id") or res.get("order_id") or res.get("client_order_id") or stop_cid
                t["protect_order_id"] = str(oid)
                t["protect_client_order_id"] = stop_cid
                t["protect_submitted_ts"] = _now()
                t["state"] = "PROTECT_SUBMITTED"
                self._persist(
                    t,
                    "TRADE_PROTECT_SUBMITTED",
                    extra={"order_id": t["protect_order_id"], "stop_price": stop_price},
                )
            except Exception as e:
                await self._flatten_and_abort(adapter, t, kind, sym, qty, flatten_cid, f"protect.place_order failed: {e}")
            return

        # 4) PROTECT_SUBMITTED -> confirm protection exists, else flatten+abort
        if state == "PROTECT_SUBMITTED":
            start_ts = _as_float(t.get("protect_submitted_ts")) or _now()
            if (_now() - start_ts) > self.protect_timeout:
                await self._flatten_and_abort(adapter, t, kind, sym, qty, flatten_cid, f"protect confirm timeout after {self.protect_timeout}s")
                return

            ok = await self._confirm_protection(aid, stop_cid)
            if not ok:
                return

            t["protected_ts"] = _now()
            t["state"] = "PROTECTED"
            self._persist(t, "TRADE_PROTECTED", extra={"stop_client_order_id": stop_cid})
            return

    async def _detect_entry_fill(self, aid: str, sym: str, entry_cid: str, qty: float) -> Tuple[bool, Optional[float]]:
        # 1) orders (active + closed)
        try:
            open_orders = await self._list_orders_for_account_async(aid, status="active")
            closed_orders = await self._list_orders_for_account_async(aid, status="closed")
            for o in (open_orders or []) + (closed_orders or []):
                if str(o.get("client_order_id") or "") == entry_cid or str(o.get("id") or "") == entry_cid:
                    if _order_filled(o, qty):
                        ap = _as_float(o.get("filled_avg_price"))
                        return True, ap
        except Exception:
            pass

        # 2) position fallback
        try:
            snap = self._snapshot_for_account(aid)
            pos = _find_pos(snap, sym)
            if pos:
                pqty = _as_float(pos.get("qty")) or 0.0
                if pqty >= (qty - 1e-9):
                    ap = _as_float(pos.get("avg_price"))
                    if ap and ap > 0:
                        return True, ap
        except Exception:
            pass

        return False, None

    async def _confirm_protection(self, aid: str, stop_cid: str) -> bool:
        try:
            open_orders = await self._list_orders_for_account_async(aid, status="active")
            for o in open_orders or []:
                if str(o.get("client_order_id") or "") == stop_cid or str(o.get("id") or "") == stop_cid:
                    st = _order_status(o)
                    if st and st not in ("canceled", "cancelled", "rejected", "expired", "failed"):
                        return True
        except Exception:
            pass
        return False

    async def _flatten_and_abort(
        self,
        adapter: Any,
        t: Dict[str, Any],
        kind: str,
        sym: str,
        qty: float,
        flatten_cid: str,
        reason: str,
    ) -> None:
        try:
            close_side = _opp_side_for_close(kind)
            payload = {
                "symbol": sym,
                "side": close_side,
                "qty": qty,
                "type": "market",
                "time_in_force": "day",
                "extended_hours": bool(t.get("extended_hours", False)),
                "note": f"ABORT_PROTECTION:{reason}",
                "client_order_id": flatten_cid,
            }
            res = await _maybe_await(getattr(adapter, "place_order", None), **payload)
            t["flatten_order_id"] = res.get("id") or res.get("order_id") or res.get("client_order_id") or flatten_cid
        except Exception as e:
            t["flatten_error"] = f"flatten failed: {e}"

        t["state"] = "ABORT_PROTECTION"
        t["error"] = reason
        self._persist(t, "TRADE_ABORT_PROTECTION", extra={"reason": reason, "flatten_order_id": t.get("flatten_order_id")})
