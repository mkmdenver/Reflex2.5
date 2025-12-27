# trader/adapters/alpaca_adapter.py
# v1.4 — normalize open orders so local echo merges correctly
#
# ONLY CHANGE vs your v1.3:
#   - list_open_orders() now normalizes orders via self._normalize_order(...)
#
# No other logic altered.

from __future__ import annotations
import logging
from typing import Optional, Dict, Any, List

import httpx
from .types import AccountSnapshot, Position

log = logging.getLogger("trader.adapters.alpaca")


class AlpacaAdapter:
    broker_id = "alpaca"
    kind = "rest"

    def __init__(
        self,
        account_id: str,
        base: Optional[str],
        key_id: Optional[str],
        secret: Optional[str],
    ) -> None:
        self.account_id = account_id
        self.base = (base or "").rstrip("/") or "https://paper-api.alpaca.markets"
        self.key_id = key_id or ""
        self.secret = secret or ""

        log.info(
            "alpaca.init account_id=%r base=%r key_present=%s secret_present=%s",
            self.account_id,
            self.base,
            bool(self.key_id),
            bool(self.secret),
        )

    def _normalize_order(self, o: dict) -> dict:
        def f(x):
            try:
                return float(x) if x is not None else None
            except Exception:
                return None

        def fi(x):
            try:
                return float(x) if x is not None else 0.0
            except Exception:
                return 0.0

        return {
            "id": o.get("id") or o.get("order_id") or o.get("client_order_id"),
            "client_order_id": o.get("client_order_id"),
            "symbol": o.get("symbol"),
            "side": o.get("side"),
            "qty": fi(o.get("qty")),
            "type": o.get("type") or o.get("order_type"),
            "time_in_force": o.get("time_in_force"),
            "limit_price": f(o.get("limit_price")),
            "stop_price": f(o.get("stop_price")),
            "status": o.get("status"),
            "submitted_at": o.get("submitted_at") or o.get("created_at"),
            "filled_at": o.get("filled_at"),
            "canceled_at": o.get("canceled_at"),
            "expired_at": o.get("expired_at"),
            "failed_at": o.get("failed_at"),
            "filled_qty": fi(o.get("filled_qty")),
            "filled_avg_price": f(o.get("filled_avg_price")),
            "extended_hours": bool(o.get("extended_hours", False)),
        }

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _headers(self) -> Dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self.secret,
            "Accept": "application/json",
            "User-Agent": "ReflexTrader/1.0",
        }

    def _has_creds(self) -> bool:
        return bool(self.base and self.key_id and self.secret)

    # ------------------------------------------------------------------ #
    # Snapshot
    # ------------------------------------------------------------------ #

    def refresh_snapshot(self) -> AccountSnapshot:
        if not self._has_creds():
            log.warning(
                "alpaca.refresh_snapshot: missing creds for %r", self.account_id
            )
            return AccountSnapshot(self.account_id, 0.0, 0.0, 0.0, [])

        cash = equity = buying_power = 0.0
        positions: List[Position] = []

        try:
            with httpx.Client(timeout=15.0) as client:
                acc = client.get(f"{self.base}/v2/account", headers=self._headers())
                acc.raise_for_status()
                adata = acc.json()
                cash = float(adata.get("cash", 0) or 0.0)
                equity = float(adata.get("equity", 0) or 0.0)
                buying_power = float(adata.get("buying_power", 0) or 0.0)

                pos = client.get(f"{self.base}/v2/positions", headers=self._headers())
                if pos.status_code == 200:
                    for p in pos.json():
                        try:
                            positions.append(
                                Position(
                                    symbol=p.get("symbol", ""),
                                    qty=float(p.get("qty", 0) or 0.0),
                                    avg_price=float(p.get("avg_entry_price", 0) or 0.0),
                                )
                            )
                        except Exception:
                            log.exception("alpaca.refresh_snapshot: bad position %r", p)
        except Exception:
            log.exception("alpaca.refresh_snapshot failed")

        return AccountSnapshot(self.account_id, cash, equity, buying_power, positions)

    # ------------------------------------------------------------------ #
    # Order placement
    # ------------------------------------------------------------------ #

    def place_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        type: str = "market",
        time_in_force: str = "day",
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        trail: Optional[float] = None,
        extended_hours: bool = False,
        note: str = "",
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self._has_creds():
            raise RuntimeError(
                f"alpaca.place_order: missing creds for {self.account_id}"
            )

        sym = (symbol or "").upper()
        s_side = (side or "").lower()
        if not sym or not s_side or not qty:
            raise ValueError("symbol, side, qty are required")

        alp_type = type or "market"
        if alp_type == "trailing":
            alp_type = "trailing_stop"

        payload: Dict[str, Any] = {
            "symbol": sym,
            "side": s_side,
            "type": alp_type,
            "qty": qty,
            "time_in_force": time_in_force or "day",
            "extended_hours": bool(extended_hours),
        }

        if limit_price is not None:
            payload["limit_price"] = float(limit_price)
        if stop_price is not None:
            payload["stop_price"] = float(stop_price)
        if alp_type == "trailing_stop" and trail is not None:
            payload["trail_price"] = float(trail)
        if client_order_id:
            payload["client_order_id"] = client_order_id

        log.info(
            "alpaca.place_order account_id=%r symbol=%s side=%s qty=%s type=%s tif=%s limit=%s stop=%s ext=%s cid=%s",
            self.account_id,
            sym,
            s_side,
            qty,
            alp_type,
            payload["time_in_force"],
            payload.get("limit_price"),
            payload.get("stop_price"),
            payload["extended_hours"],
            client_order_id or "",
        )

        url = f"{self.base}/v2/orders"
        with httpx.Client(timeout=15.0) as client:
            r = client.post(url, headers=self._headers(), json=payload)
            try:
                body = r.json()
            except Exception:
                body = {"raw": r.text}

            if r.status_code >= 400:
                msg = body.get("message") if isinstance(body, dict) else None
                log.error(
                    "alpaca.place_order HTTP error account_id=%r status=%s body=%r",
                    self.account_id,
                    r.status_code,
                    body,
                )
                raise RuntimeError(f"Alpaca {r.status_code}: {msg or body}")

            return body

    # ------------------------------------------------------------------ #
    # Orders listing
    # ------------------------------------------------------------------ #

    def list_open_orders(self, status: str = "open") -> List[Dict[str, Any]]:
        if not self._has_creds():
            return []

        alp_status = {
            "open": "open",
            "working": "open",
            "filled": "closed",
            "canceled": "closed",
            "closed": "closed",
        }.get((status or "open").lower(), "open")

        url = f"{self.base}/v2/orders?status={alp_status}&limit=100&nested=true"
        with httpx.Client(timeout=15.0) as client:
            r = client.get(url, headers=self._headers())
            try:
                r.raise_for_status()
            except Exception:
                log.error("alpaca.list_open_orders HTTP error %s", r.text)
                return []

            try:
                data = r.json()
                if not isinstance(data, list):
                    return []
                # ✅ FIX: normalize open orders too (so client_order_id is present)
                out: List[Dict[str, Any]] = []
                for o in data:
                    out.append(self._normalize_order(o))
                return out
            except Exception:
                log.exception("alpaca.list_open_orders invalid JSON")
                return []

    def list_closed_orders(self, limit: int = 50) -> List[Dict[str, Any]]:
        if not self._has_creds():
            return []

        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(
                    f"{self.base}/v2/orders",
                    headers=self._headers(),
                    params={
                        "status": "closed",
                        "limit": int(limit),
                        "direction": "desc",
                    },
                )
                if resp.status_code != 200:
                    log.warning("alpaca.list_closed_orders HTTP error %s", resp.text)
                    return []
                out: List[Dict[str, Any]] = []
                for o in resp.json() or []:
                    try:
                        out.append(self._normalize_order(o))
                    except Exception:
                        log.exception("alpaca.list_closed_orders bad order %r", o)
                return out
        except Exception:
            log.exception("alpaca.list_closed_orders failed")
            return []

    # ------------------------------------------------------------------ #
    # Cancel
    # ------------------------------------------------------------------ #

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        if not self._has_creds():
            raise RuntimeError(
                f"alpaca.cancel_order: missing creds for {self.account_id}"
            )

        url = f"{self.base}/v2/orders/{order_id}"
        with httpx.Client(timeout=15.0) as client:
            r = client.delete(url, headers=self._headers())
            if r.status_code in (200, 204):
                return {"status": "cancelled", "order_id": order_id}
            try:
                body = r.json()
            except Exception:
                body = r.text
            log.error("alpaca.cancel_order HTTP error %r", body)
            return {"status": "error", "order_id": order_id, "response": body}

    def close(self) -> None:
        return
