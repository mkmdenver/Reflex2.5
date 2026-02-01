# trader/adapters/alpaca_adapter.py
# v1.5 — include position market fields (current_price/unrealized_pl/etc) in snapshots
#
# v1.5 changes:
#   - refresh_snapshot() now maps Alpaca /v2/positions fields into Position:
#       market_price, market_value, unrealized_pl, unrealized_plpc, side
#   - No other behavior changed.

from __future__ import annotations
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
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

    def _has_creds(self) -> bool:
        return bool(self.key_id and self.secret)

    def _headers(self) -> Dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self.secret,
        }

    def _round_price(self, price: float, *, side: str, purpose: str) -> Decimal:
        """
        Alpaca enforces tick increments depending on venue. We keep it simple:
        - ROUND_DOWN for sells (more aggressive)
        - ROUND_UP for buys (more aggressive)
        """
        d = Decimal(str(price))
        if purpose == "limit":
            if (side or "").lower() == "sell":
                return d.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            return d.quantize(Decimal("0.01"), rounding=ROUND_UP)

        # stop prices can be rounded normally
        return d.quantize(Decimal("0.01"), rounding=ROUND_UP)

    # ------------------------------------------------------------------ #
    # Snapshot
    # ------------------------------------------------------------------ #

    def refresh_snapshot(self) -> AccountSnapshot:
        if not self._has_creds():
            raise RuntimeError(f"alpaca.refresh_snapshot: missing creds for {self.account_id}")

        # account
        url = f"{self.base}/v2/account"
        with httpx.Client(timeout=15.0) as client:
            r = client.get(url, headers=self._headers())
            r.raise_for_status()
            acct = r.json()

        cash = float(acct.get("cash") or 0)
        equity = float(acct.get("equity") or 0)
        buying_power = float(acct.get("buying_power") or 0)

        # positions
        url = f"{self.base}/v2/positions"
        with httpx.Client(timeout=15.0) as client:
            r = client.get(url, headers=self._headers())
            if r.status_code == 404:
                pos_list = []
            else:
                r.raise_for_status()
                pos_list = r.json()

        positions: List[Position] = []
        for p in pos_list or []:
            try:
                sym = str(p.get("symbol") or "").upper()
                qty = float(p.get("qty") or 0)
                avg_price = float(p.get("avg_entry_price") or p.get("avg_price") or 0)

                # New fields
                market_price = float(p.get("current_price") or p.get("market_price") or 0) if p.get("current_price") is not None or p.get("market_price") is not None else None
                market_value = float(p.get("market_value") or 0) if p.get("market_value") is not None else None
                unrealized_pl = float(p.get("unrealized_pl") or 0) if p.get("unrealized_pl") is not None else None
                unrealized_plpc = float(p.get("unrealized_plpc") or 0) if p.get("unrealized_plpc") is not None else None

                side = "long"
                try:
                    if qty < 0:
                        side = "short"
                except Exception:
                    pass

                positions.append(
                    Position(
                        symbol=sym,
                        qty=qty,
                        avg_price=avg_price,
                        market_price=market_price,
                        market_value=market_value,
                        unrealized_pl=unrealized_pl,
                        unrealized_plpc=unrealized_plpc,
                        side=side,
                    )
                )
            except Exception:
                continue

        return AccountSnapshot(
            account_id=self.account_id,
            cash=cash,
            equity=equity,
            buying_power=buying_power,
            positions=positions,
        )

    # ------------------------------------------------------------------ #
    # Orders
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
            raise RuntimeError(f"alpaca.place_order: missing creds for {self.account_id}")

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
            payload["limit_price"] = float(self._round_price(limit_price, side=s_side, purpose="limit"))
        if stop_price is not None:
            payload["stop_price"] = float(self._round_price(stop_price, side=s_side, purpose="stop"))
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

            if isinstance(body, dict):
                try:
                    body["submit_ts"] = time.time()
                    body["submit_iso_utc"] = datetime.now(timezone.utc).isoformat()
                except Exception:
                    pass
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
            "cancelled": "closed",
            "closed": "closed",
        }.get((status or "open").lower(), "open")

        url = f"{self.base}/v2/orders?status={alp_status}&direction=desc&limit=200"
        with httpx.Client(timeout=15.0) as client:
            r = client.get(url, headers=self._headers())
            if r.status_code >= 400:
                return []
            try:
                arr = r.json()
            except Exception:
                return []
        return arr if isinstance(arr, list) else []
