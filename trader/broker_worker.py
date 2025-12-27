# trader/broker_worker.py
# Version: 2025-12-04
# Purpose:
#   Bridge Evaluator "order intents" on Redis -> Trader HTTP /v1/orders.
#   Runs as a small, Windows-friendly async worker with good logging.
#
# Notes:
#   • Supports BOTH legacy "flat" intents and new envelope-shaped messages:
#         { "symbol": "...", "side": "...", ... }
#      or:
#         { "intent": { ... }, "meta": { "model": "...", "strength": 1.0, ... } }
#   • account_id always comes from the intent payload; a single Trader process
#     can talk to many broker accounts and receive intents from many bots.

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import argparse
import platform
import signal
from typing import Optional, Dict, Any

import httpx

from common.bus import subscribe, unpack, CHANNELS
from .core import get_logger, get_instance_id, get_redis_url, get_trader_api_port

log = get_logger("trader")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trader_base_url() -> str:
    """Prefer explicit TRADER_BASE, fall back to localhost:TRADER_API_PORT."""
    base = os.getenv("TRADER_BASE")
    if not base:
        port = get_trader_api_port()
        base = f"http://127.0.0.1:{port}"
    return base.rstrip("/")


# ---------------------------------------------------------------------------
# Intent forwarding
# ---------------------------------------------------------------------------

async def _forward_intent_to_trader(
    intent: Dict[str, Any],
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Take a normalized intent from Redis and POST it to Trader /v1/orders.

    We log both the outgoing payload and any error body from Trader so
    that we can debug things like "adapter does not support place_order".
    """
    meta = meta or {}

    # account_id is supplied by the eval bot / strategy; this is how one
    # Trader instance can talk to many accounts.
    account_id = (intent.get("account_id") or "").strip()
    if not account_id:
        # Fallback: env default, then sim:cash for safety.
        account_id = os.getenv("TRADER_DEFAULT_ACCOUNT_ID", "sim:cash")

    symbol = intent.get("symbol")
    side = intent.get("side")
    qty = intent.get("qty")

    # Minimal sanity log of what we think we're doing
    log.info(
        "broker_worker.intent.forward",
        extra={
            "account_id": account_id,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "meta_model": meta.get("model"),
            "meta_strength": meta.get("strength"),
            "meta_risk": meta.get("risk"),
        },
    )

    if not symbol or not side or not qty:
        log.error(
            "broker_worker.intent.missing_fields",
            extra={
                "account_id": account_id,
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "intent": intent,
            },
        )
        return

    payload: Dict[str, Any] = {
        "account_id": account_id,
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "type": intent.get("type", "market"),
        "time_in_force": intent.get("time_in_force")
                         or intent.get("tif")
                         or "day",
        "limit_price": intent.get("limit_price"),
        "stop_price": intent.get("stop_price"),
        "trail": intent.get("trail"),
        "extended_hours": bool(intent.get("extended_hours", False)),
        "note": intent.get("note") or intent.get("reason") or "",
    }

    # Attach a free-form intent_id if present for traceability.
    if intent.get("intent_id"):
        payload["intent_id"] = intent["intent_id"]

    url = _trader_base_url() + "/v1/orders"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
    except Exception as exc:
        log.exception(
            "broker_worker.intent.http_error",
            extra={"account_id": account_id, "error": repr(exc), "url": url},
        )
        return

    # Always try to capture the body for debugging
    try:
        body: Any = resp.json()
    except Exception:
        with contextlib.suppress(Exception):
            body = resp.text  # type: ignore[assignment]
        if body is None:
            body = "<no-body>"

    if resp.status_code != 200:
        log.error(
            "broker_worker.intent.trader_error",
            extra={
                "status": resp.status_code,
                "account_id": account_id,
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "payload": payload,
                "body": body,
            },
        )
        return

    log.info(
        "broker_worker.intent.applied",
        extra={
            "status": resp.status_code,
            "account_id": account_id,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "body": body,
        },
    )


# ---------------------------------------------------------------------------
# Intent loop
# ---------------------------------------------------------------------------

async def _intent_loop(instance: str, stop_event: asyncio.Event) -> None:
    channel = CHANNELS["order"]
    log.info(
        "broker_worker.intent_loop.start",
        extra={"instance": instance, "channel": channel},
    )

    ps = await subscribe(channel)

    try:
        async for msg in ps.listen():  # type: ignore[attr-defined]
            if stop_event.is_set():
                break
            if msg.get("type") != "message":
                continue

            raw = msg.get("data")
            try:
                payload = unpack(raw)
            except Exception as exc:
                log.exception(
                    "broker_worker.intent.unpack_error",
                    extra={"error": repr(exc)},
                )
                continue

            # Support both:
            #   • legacy flat intents: payload is the intent
            #   • new envelopes: { "intent": {...}, "meta": {...} }
            meta: Dict[str, Any] = {}
            if isinstance(payload, dict) and "intent" in payload:
                inner = payload.get("intent") or {}
                if not isinstance(inner, dict):
                    log.error(
                        "broker_worker.intent.bad_envelope",
                        extra={"payload_type": type(payload).__name__},
                    )
                    continue
                intent = inner
                meta = payload.get("meta") or {}
                if not isinstance(meta, dict):
                    meta = {}
            else:
                intent = payload  # legacy case

            if not isinstance(intent, dict):
                log.error(
                    "broker_worker.intent.bad_payload_type",
                    extra={"payload_type": type(intent).__name__},
                )
                continue

            # Quick breadcrumb for traceability; payload details are in
            # _forward_intent_to_trader().
            log.debug(
                "broker_worker.intent.recv",
                extra={
                    "instance": instance,
                    "account_id": intent.get("account_id"),
                    "symbol": intent.get("symbol"),
                    "side": intent.get("side"),
                    "qty": intent.get("qty"),
                    "meta_model": meta.get("model"),
                    "meta_strength": meta.get("strength"),
                    "meta_risk": meta.get("risk"),
                },
            )

            await _forward_intent_to_trader(intent, meta)
    finally:
        with contextlib.suppress(Exception):
            await ps.unsubscribe(channel)
        with contextlib.suppress(Exception):
            await ps.close()
        log.info(
            "broker_worker.intent_loop.stop",
            extra={"instance": instance, "channel": channel},
        )


# ---------------------------------------------------------------------------
# Runner / signal handling (Windows-safe)
# ---------------------------------------------------------------------------

async def worker_main(instance: str, stop_event: Optional[asyncio.Event] = None) -> None:
    """
    Main async body of the broker worker.

    We keep this intentionally small: set up the stop_event and run the
    Redis intent loop. All the interesting work is in _intent_loop().
    """
    if stop_event is None:
        stop_event = asyncio.Event()

    log.info(
        "broker_worker.start",
        extra={"instance": instance, "redis": get_redis_url()},
    )

    try:
        await _intent_loop(instance, stop_event)
    except asyncio.CancelledError:
        log.info(
            "broker_worker.cancelled",
            extra={"instance": instance},
        )
    except Exception as exc:
        log.exception(
            "broker_worker.error",
            extra={"instance": instance, "error": repr(exc)},
        )
    finally:
        log.info(
            "broker_worker.stop",
            extra={"instance": instance},
        )


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    """
    Try to install SIGINT/SIGTERM handlers on platforms that support it.

    On Windows with ProactorEventLoop this will raise NotImplementedError,
    in which case we just log and rely on KeyboardInterrupt (Ctrl+C).
    """
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        log.debug("broker_worker.signals.installed")
    except (NotImplementedError, RuntimeError):
        # Not available on this platform / event loop
        log.debug("broker_worker.signals.unavailable")


async def _runner(instance: str) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, stop_event)
    await worker_main(instance, stop_event)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="trader.broker_worker", add_help=True)
    p.add_argument(
        "--instance",
        default=os.getenv("REFLEX__INSTANCE")
        or os.getenv("INSTANCE")
        or get_instance_id(),
        help="logical instance id for logging/metrics",
    )
    return p.parse_args(argv)


def _main() -> int:
    args = _parse_args(sys.argv[1:])
    instance = str(args.instance)

    # Log some environment breadcrumbs (do NOT invent new names)
    log.info(
        "broker_worker.bootstrap",
        extra={
            "instance": instance,
            "redis_url": get_redis_url(),
            "trader_base": _trader_base_url(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        },
    )

    # Windows-safe asyncio entrypoint: rely on KeyboardInterrupt rather than signals
    try:
        asyncio.run(_runner(instance))
        return 0
    except KeyboardInterrupt:
        # Graceful shutdown from console Ctrl+C on Windows
        log.info("broker_worker.keyboard_interrupt", extra={"instance": instance})
        return 0
    except Exception as exc:
        log.exception(
            "broker_worker.crashed",
            extra={"instance": instance, "error": repr(exc)},
        )
        return 1


if __name__ == "__main__" or __package__ == "trader.broker_worker":
    sys.exit(_main())
