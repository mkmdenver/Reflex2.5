# trader/broker/sim.py
from __future__ import annotations
import os
import asyncio
from typing import Any, Dict, List, Optional
from decimal import Decimal, InvalidOperation

from common import logging as log
from .base import Broker

COMPONENT = __name__


def _to_dec(x: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


class BrokerSim(Broker):
    """
    Lightweight but practical simulator:

    - Immediate ACK, optional small latency before returning result.
    - Single full fill per order (keeps current event plumbing intact).
    - Price formation (no quotes needed):
        reference_price = payload.limit_price | payload.mark_price | payload.est_price
                           | payload.price | position.avg_price | SIM_FALLBACK_PRICE
        slippage_bps applied:
            BUY  -> price = min(reference * (1 + bps/10000), limit_price if provided)
            SELL -> price = max(reference * (1 - bps/10000), limit_price if provided)
      If no limit provided, just use slippage-adjusted reference.

    - Tracks cash, equity (cash + sum(qty*avg_price)), positions (qty, avg_price).
    - Buying power = cash * SIM_BP_MULT (coarse; risk is enforced upstream anyway).

    Env knobs (optional):
        SIM_START_CASH      (default 30000)
        SIM_BP_MULT         (default 2.0)
        SIM_SLIPPAGE_BPS    (default 10)   # 10 bps = 0.10%
        SIM_LATENCY_MS      (default 10)   # small sleep to mimic API latency
        SIM_FALLBACK_PRICE  (default 10.0) # used if no price hints and no position
    """

    name = "sim"

    def __init__(self, account_id: str = "sim:paper"):
        self.account_id = account_id

        self._cash: Decimal = _to_dec(os.getenv("SIM_START_CASH", "30000"))
        self._bp_mult: Decimal = _to_dec(os.getenv("SIM_BP_MULT", "2.0"))
        self._slip_bps: int = _env_int("SIM_SLIPPAGE_BPS", 10)
        self._latency_ms: int = _env_int("SIM_LATENCY_MS", 10)
        self._fallback_px: Decimal = _to_dec(os.getenv("SIM_FALLBACK_PRICE", "10.0"))

        self._positions: Dict[str, Dict[str, Any]] = {}  # symbol -> {"qty": int, "avg_price": Decimal}
        self._lock = asyncio.Lock()

        log.info(
            COMPONENT, "sim.init",
            account_id=self.account_id,
            start_cash=str(self._cash),
            bp_mult=str(self._bp_mult),
            slippage_bps=self._slip_bps,
            latency_ms=self._latency_ms,
            fallback_price=str(self._fallback_px),
        )

    # ---------------- Public surface expected by factory/worker ----------------

    async def submit(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        """
        intent = {
          "trace_id": "...",
          "payload": {
              account_id, symbol, side, order_type, qty,
              limit_price?, price?/mark_price?/est_price?,
              time_in_force?, ...
          }
        }
        """
        trace_id = intent.get("trace_id")
        p = intent.get("payload") or {}

        sym = (p.get("symbol") or "").upper()
        side = (p.get("side") or "").upper()  # BUY | SELL
        qty = int(p.get("qty") or 0)
        order_type = (p.get("order_type") or "market").lower()
        tif = (p.get("time_in_force") or "day").lower()

        if not trace_id or not sym or side not in ("BUY", "SELL") or qty <= 0:
            log.warn(COMPONENT, "sim.reject.bad_payload", trace_id=trace_id, sym=sym, side=side, qty=qty)
            raise ValueError("invalid order payload for sim")

        # Synthetic latency to mimic API roundtrip
        if self._latency_ms > 0:
            await asyncio.sleep(self._latency_ms / 1000.0)

        # Determine a reference price with no quotes available
        ref_price = self._reference_price(sym, p)
        limit_px = _to_dec(p.get("limit_price"), None) if p.get("limit_price") is not None else None

        # Slippage-adjusted execution price
        exec_px = self._apply_slippage(side, ref_price)

        # Respect limit if provided: clamp into the limit boundary
        if limit_px is not None:
            if side == "BUY":
                exec_px = min(exec_px, limit_px)
            else:  # SELL
                exec_px = max(exec_px, limit_px)

        # Build ACK
        ack = {
            "trace_id": trace_id,
            "topic": "orders.ack",
            "payload": {
                "status": "ACCEPTED",
                "broker": self.name,
                "broker_order_id": trace_id,  # convenient for downstream logs
                "client_order_id": trace_id,
                "symbol": sym,
                "side": side,
                "qty": qty,
                "type": order_type,
                "tif": tif,
            },
        }

        # Apply the fill atomically
        async with self._lock:
            self._apply_fill(sym, side, qty, exec_px)

        fill = {
            "trace_id": trace_id,
            "topic": "orders.fill",
            "payload": {
                "status": "FILLED",
                "broker": self.name,
                "client_order_id": trace_id,
                "symbol": sym,
                "side": side,
                "qty": qty,
                "price": float(exec_px),
            },
        }

        log.info(
            COMPONENT,
            "sim.fill",
            account_id=self.account_id,
            trace_id=trace_id,
            symbol=sym,
            side=side,
            qty=qty,
            exec_price=str(exec_px),
            ref_price=str(ref_price),
            limit=str(limit_px) if limit_px is not None else None,
            slippage_bps=self._slip_bps,
        )
        return {"ack": ack, "fill": fill}

    async def cancel(self, client_order_id: str) -> Dict[str, Any]:
        # No resting orders; return a benign cancel ACK
        log.info(COMPONENT, "sim.cancel", account_id=self.account_id, coid=client_order_id)
        return {
            "trace_id": client_order_id,
            "topic": "orders.ack",
            "payload": {"status": "CANCELLED", "broker": self.name, "client_order_id": client_order_id},
        }

    async def fetch_open_orders(self) -> List[Dict[str, Any]]:
        # Immediate fills mean no open orders
        return []

    async def fetch_positions(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        async with self._lock:
            for sym, p in self._positions.items():
                out.append({"symbol": sym, "qty": p["qty"], "avg_price": float(p["avg_price"])})
        return out

    async def close(self):
        return

    # ---------------------------- Internals ----------------------------

    def _reference_price(self, symbol: str, payload: Dict[str, Any]) -> Decimal:
        """
        Establish a reference price hierarchy with no quote stream:
        limit_price > mark_price > est_price > price > position.avg > SIM_FALLBACK_PRICE
        """
        hints = (
            payload.get("limit_price"),
            payload.get("mark_price"),
            payload.get("est_price"),
            payload.get("price"),
        )
        for h in hints:
            v = _to_dec(h, None) if h is not None else None
            if v is not None and v > 0:
                return v

        # No hints — use current position avg as a rough anchor if present
        pos = self._positions.get(symbol)
        if pos:
            ap = _to_dec(pos.get("avg_price"), None)
            if ap is not None and ap > 0:
                return ap

        # Final fallback
        return self._fallback_px

    def _apply_slippage(self, side: str, ref_price: Decimal) -> Decimal:
        """
        Simple bps slippage model:
            BUY  -> worse by +bps
            SELL -> worse by -bps
        """
        bps = Decimal(self._slip_bps) / Decimal(10000)
        if ref_price <= 0:
            ref_price = self._fallback_px
        if side == "BUY":
            px = ref_price * (Decimal(1) + bps)
        else:
            px = ref_price * (Decimal(1) - bps)
        # keep to 4 decimal places like most equities
        return px.quantize(Decimal("0.0001"))

    def _apply_fill(self, symbol: str, side: str, qty: int, price: Decimal) -> None:
        """
        Update cash and positions with a full fill.
        Longs/shorts handled via signed quantity.
        """
        if price < 0:
            price = Decimal("0")

        pos = self._positions.get(symbol) or {"qty": 0, "avg_price": Decimal("0")}
        curr_qty = int(pos["qty"])
        avg = _to_dec(pos["avg_price"])

        notional = price * Decimal(qty)

        # Cash update: BUY consumes, SELL releases
        if side == "BUY":
            self._cash -= notional
        else:
            self._cash += notional

        # Position update
        if side == "BUY":
            if curr_qty >= 0:
                # grow/open long
                new_qty = curr_qty + qty
                new_cost = avg * Decimal(curr_qty) + notional
                new_avg = (new_cost / Decimal(new_qty)) if new_qty != 0 else Decimal("0")
                curr_qty, avg = new_qty, new_avg
            else:
                # cover short
                new_qty = curr_qty + qty
                curr_qty = new_qty
                if curr_qty == 0:
                    avg = Decimal("0")
        else:  # SELL
            if curr_qty <= 0:
                # grow/open short (store avg as positive magnitude for readability)
                new_qty = curr_qty - qty
                new_cost = avg * Decimal(abs(curr_qty)) + notional
                new_avg = (new_cost / Decimal(abs(new_qty))) if new_qty != 0 else Decimal("0")
                curr_qty, avg = new_qty, new_avg
            else:
                # reduce long
                new_qty = curr_qty - qty
                curr_qty = new_qty
                if curr_qty == 0:
                    avg = Decimal("0")

        self._positions[symbol] = {"qty": curr_qty, "avg_price": avg}

    # ---------------- Convenience snapshot (not used by worker) ----------------

    def account_snapshot(self) -> Dict[str, Any]:
        equity = self._cash
        for p in self._positions.values():
            q = int(p["qty"])
            ap = _to_dec(p["avg_price"])
            equity += ap * Decimal(q)
        bp = self._cash * self._bp_mult
        return {
            "account_id": self.account_id,
            "broker": self.name,
            "cash": float(self._cash),
            "buying_power": float(bp),
            "equity": float(equity),
            "positions": [
                {"symbol": s, "qty": v["qty"], "avg_price": float(_to_dec(v["avg_price"]))}
                for s, v in self._positions.items()
            ],
            "open_orders": [],
        }
