
# trader/alpaca_trade_updates.py
# v1.0 — Alpaca trade-updates (order lifecycle) event listener.
#
# Design:
# - We do NOT compute balances/positions.
# - We listen for broker events, then trigger immediate REST refresh (broker is truth).
# - A slower reconcile loop remains as drift correction.
#
# Notes:
# Alpaca trade update stream is typically:
#   wss://paper-api.alpaca.markets/stream   (paper)
#   wss://api.alpaca.markets/stream         (live)
# Auth message:
#   {"action":"authenticate","data":{"key_id": "...", "secret_key":"..."}}
# Listen message:
#   {"action":"listen","data":{"streams":["trade_updates"]}}
#
# This module is intentionally defensive: if endpoint differs, you'll see logs
# and the reconcile loop still keeps the system safe (but less responsive).

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Callable, Dict, Optional

try:
    import websocket  # websocket-client
except Exception:  # pragma: no cover
    websocket = None  # type: ignore

log = logging.getLogger("trader.alpaca.events")


def _https_to_wss(base_https: str) -> str:
    base = (base_https or "").strip().rstrip("/")
    if not base:
        base = "https://paper-api.alpaca.markets"
    # Replace scheme
    base = re.sub(r"^http://", "ws://", base)
    base = re.sub(r"^https://", "wss://", base)
    return base + "/stream"


async def listen_trade_updates(
    *,
    account_id: str,
    base_https: str,
    key_id: str,
    secret: str,
    on_event: Callable[[Dict[str, Any]], None],
    stop_event: asyncio.Event,
    stream_url: Optional[str] = None,
) -> None:
    """Connect to Alpaca trade_updates stream and invoke on_event for each event message."""
    if websocket is None:
        log.error("websocket-client not installed; cannot run alpaca events")
        return

    url = (stream_url or _https_to_wss(base_https)).strip()
    if not url:
        url = _https_to_wss(base_https)

    auth_msg = {"action": "authenticate", "data": {"key_id": key_id, "secret_key": secret}}
    listen_msg = {"action": "listen", "data": {"streams": ["trade_updates"]}}

    def _run_blocking() -> None:
        ws = None
        try:
            log.info("connect account_id=%s url=%s", account_id, url)
            ws = websocket.create_connection(url, timeout=20)
            ws.send(json.dumps(auth_msg))
            ws.send(json.dumps(listen_msg))
            ws.settimeout(5)

            while not stop_event.is_set():
                try:
                    raw = ws.recv()
                except Exception:
                    continue
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    log.debug("bad_json account_id=%s raw=%r", account_id, raw)
                    continue

                # Alpaca may send {"stream":"trade_updates","data":{...}} or {"data":..., "stream":...}
                if isinstance(msg, dict) and msg.get("stream") == "trade_updates":
                    data = msg.get("data") or {}
                    if isinstance(data, dict):
                        try:
                            on_event(data)
                        except Exception:
                            log.exception("on_event failed account_id=%s", account_id)
                # Sometimes API sends list of messages
                elif isinstance(msg, list):
                    for item in msg:
                        if isinstance(item, dict) and item.get("stream") == "trade_updates":
                            data = item.get("data") or {}
                            if isinstance(data, dict):
                                try:
                                    on_event(data)
                                except Exception:
                                    log.exception("on_event failed account_id=%s", account_id)
        except Exception:
            log.exception("events.loop crashed account_id=%s", account_id)
        finally:
            try:
                if ws is not None:
                    ws.close()
            except Exception:
                pass
            log.info("disconnect account_id=%s", account_id)

    await asyncio.to_thread(_run_blocking)
