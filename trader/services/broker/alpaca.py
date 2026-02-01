# trader/broker/alpaca.py
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from common import logging as log

from trader.adapters.alpaca_http import AlpacaHttpAdapter
from trader.db_brokers import get_broker_creds

COMPONENT = "trader.broker.alpaca"


class BrokerAlpaca:
    """
    Async-facing adapter that the BrokerWorker calls.
    Internally delegates to the *sync* AlpacaHttpAdapter via asyncio.to_thread,
    so BrokerWorker stays async while all HTTP is centralized.

    submit(intent) returns {"ack": {...}} shape (and may include "fill" in future).
    cancel(client_order_id) returns an ACK-shaped dict.
    fetch_open_orders() returns normalized list for warm-ups.
    """

    broker = "alpaca"

    def __init__(self, account_id: str, api_base: str, api_key_id: str, api_secret_key: str):
        self.account_id = account_id
        self._http = AlpacaHttpAdapter(
            account_id=account_id,
            api_base=api_base,
            api_key_id=api_key_id,
            api_secret_key=api_secret_key,
        )
        log.info(COMPONENT, "init", account_id=account_id, base=api_base)

    # ---------------- public (async) ----------------

    async def submit(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        """
        intent = {
            "trace_id": "...",
            "payload": {
                "account_id", "symbol", "side", "order_type", "qty",
                "limit_price?", "stop_price?", "time_in_force?", "extended_hours?"
            }
        }
        """
        payload = dict(intent.get("payload") or {})
        coid = intent.get("trace_id")
        if coid and "coid" not in payload:
            payload["coid"] = coid

        # Route through unified HTTP adapter
        return await asyncio.to_thread(self._http.place_order, payload)

    async def cancel(self, client_order_id: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self._http.cancel_order, client_order_id)

    async def fetch_open_orders(self) -> List[Dict[str, Any]]:
        # AlpacaHttpAdapter doesn't expose a public list method; use reconcile() and slice
        def _list_open() -> List[Dict[str, Any]]:
            snap = self._http.reconcile()
            return snap.get("open_orders", []) or []
        return await asyncio.to_thread(_list_open)

    async def close(self) -> None:
        # Nothing to close for requests-based client (kept for symmetry with other adapters)
        return None


# --------- helper to build from DB creds (used by factory) ---------

def build_from_db(account_id: str) -> BrokerAlpaca:
    """
    Read creds from broker_credentials for 'alpaca'.
    Expected keys: base_url, api_key (or api_key_id), api_secret (or api_secret_key).
    """
    creds = get_broker_creds("alpaca") or {}
    base = (
        creds.get("base_url")
        or creds.get("api_base")  # tolerate older naming
    )
    key = creds.get("api_key") or creds.get("api_key_id")
    sec = creds.get("api_secret") or creds.get("api_secret_key")

    missing = [k for k, v in (("base_url", base), ("api_key", key), ("api_secret", sec)) if not v]
    if missing:
        msg = f"Missing Alpaca credential(s) for {account_id}: {', '.join(missing)}"
        log.error(COMPONENT, "missing_creds", account_id=account_id, missing=",".join(missing))
        raise RuntimeError(msg)

    return BrokerAlpaca(account_id=account_id, api_base=base, api_key_id=key, api_secret_key=sec)
