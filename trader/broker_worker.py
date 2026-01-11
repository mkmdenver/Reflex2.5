# trader/broker_worker.py
# Version: 2026-01-09 (patched)
# Purpose:
#   Bridge Evaluator intents on Redis -> Trader HTTP /v1/intents (NEW standard).
#
# Notes:
#   • Supports BOTH legacy "order-ish" intents and new Intent-v1 messages:
#       Legacy flat:
#         { "symbol": "...", "side": "...", "qty": 10, "type": "market", ... }
#       Envelope:
#         { "intent": { ... }, "meta": { "model": "...", ... } }
#       New Intent v1:
#         { "intent_id": "...", "symbol": "...", "side": "...", "strength": 0.7,
#           "strategy_id": "...", "risk_hints": {...}, ... }
#   • qty/shares are NOT required here anymore. Trader is the authority that
#     sizes (Risk/Capital) and compiles orders.
#   • account_id is optional (directed intent). If missing, Trader assigns.

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import platform
import signal
import sys
import time
from typing import Optional, Dict, Any, Tuple

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


def _intent_endpoint_path() -> str:
    # Default to NEW canonical endpoint
    return os.getenv("TRADER_INTENT_PATH", "/v1/intents").strip() or "/v1/intents"


def _utc_iso_now() -> str:
    # Good enough for tracing. Trader may re-stamp.
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int((time.time() % 1) * 1000):03d}Z"


def _normalize_envelope(payload: Any) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """
    Normalize incoming bus payload into (intent_dict, meta_dict).
    Supports:
      - legacy flat: payload is the intent
      - envelope: {intent:{...}, meta:{...}}
    """
    meta: Dict[str, Any] = {}

    if isinstance(payload, dict) and "intent" in payload:
        inner = payload.get("intent") or {}
        if not isinstance(inner, dict):
            return None, {}
        intent = inner
        m = payload.get("meta") or {}
        if isinstance(m, dict):
            meta = m
        return intent, meta

    if isinstance(payload, dict):
        return payload, meta

    return None, {}


def _legacy_to_intent_v1(intent: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert legacy order-ish intent into Intent v1.
    Legacy fields (qty/type/tif) become hints in meta, not requirements.
    """
    sym = intent.get("symbol")
    side = intent.get("side")

    out: Dict[str, Any] = {
        "intent_id": intent.get("intent_id") or intent.get("id") or "",
        "ts": intent.get("ts") or _utc_iso_now(),
        "symbol": sym,
        "side": side,
        "source": intent.get("source") or "bot",
        "strategy_id": intent.get("strategy_id") or meta.get("model") or "unknown",
        "strength": intent.get("strength") or meta.get("strength") or 0.5,
        "urgency": intent.get("urgency") or "normal",
        "trigger": intent.get("trigger") or {"kind": "immediate"},
        "reason": intent.get("reason") or intent.get("note") or "",
        "tags": intent.get("tags") or ([meta.get("model")] if meta.get("model") else []),
    }

    if intent.get("account_id"):
        out["account_id"] = intent.get("account_id")

    legacy_hints: Dict[str, Any] = {}
    if intent.get("qty") is not None:
        legacy_hints["requested_qty_legacy"] = intent.get("qty")
    if intent.get("type"):
        legacy_hints["order_type_legacy"] = intent.get("type")
    tif = intent.get("time_in_force") or intent.get("tif")
    if tif:
        legacy_hints["tif_legacy"] = tif
    if intent.get("extended_hours") is not None:
        legacy_hints["extended_hours_legacy"] = bool(intent.get("extended_hours"))

    if legacy_hints:
        out["meta"] = {"legacy": legacy_hints}

    return out


def _ensure_intent_v1(intent: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure outgoing payload is Intent v1.
    - If legacy order-ish, convert.
    - If already v1-ish, keep and attach envelope meta.
    """
    if ("qty" in intent) or ("time_in_force" in intent) or ("tif" in intent) or ("type" in intent):
        return _legacy_to_intent_v1(intent, meta)

    out = dict(intent)
    out.setdefault("ts", _utc_iso_now())

    if meta:
        out.setdefault("meta", {})
        if isinstance(out["meta"], dict):
            out["meta"].setdefault("model", meta.get("model"))
            for k, v in meta.items():
                if k not in out["meta"]:
                    out["meta"][k] = v

    return out


async def _ps_aclose(ps: Any) -> None:
    """
    redis-py deprecated close(); new is aclose().
    Use whichever exists to avoid warnings.
    """
    if hasattr(ps, "aclose"):
        await ps.aclose()
    else:
        await ps.close()


# ---------------------------------------------------------------------------
# Intent forwarding (NEW: /v1/intents)
# ---------------------------------------------------------------------------

async def _forward_intent_to_trader(intent_v1: Dict[str, Any]) -> None:
    """
    POST Intent v1 to Trader /v1/intents (or env override).
    Trader is responsible for Risk sizing + Plan compilation + execution.
    """
    symbol = intent_v1.get("symbol")
    side = intent_v1.get("side")
    intent_id = intent_v1.get("intent_id") or ""

    log.info(
        "broker_worker.intent.forward",
        extra={
            "symbol": symbol,
            "side": side,
            "intent_id": intent_id,
            "strategy_id": intent_v1.get("strategy_id"),
            "strength": intent_v1.get("strength"),
            "account_id": intent_v1.get("account_id"),
        },
    )

    if not symbol or not side:
        log.error(
            "broker_worker.intent.missing_fields",
            extra={"symbol": symbol, "side": side, "intent": intent_v1},
        )
        return

    url = _trader_base_url() + _intent_endpoint_path()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=intent_v1)
    except Exception as exc:
        log.exception(
            "broker_worker.intent.http_error",
            extra={"error": repr(exc), "url": url, "intent_id": intent_id, "symbol": symbol},
        )
        return

    # Always capture body text for debugging (Trader 400/422 etc.)
    resp_text: str = ""
    with contextlib.suppress(Exception):
        resp_text = resp.text

    # Try JSON body too (FastAPI validation errors are usually JSON)
    body: Any = None
    with contextlib.suppress(Exception):
        body = resp.json()

    if resp.status_code != 200:
        log.error(f"broker_worker.intent.trader_error status={resp.status_code} url={url} text={resp_text}")

        log.error(
            "broker_worker.intent.trader_error",
            extra={
                "status": resp.status_code,
                "url": url,
                "intent_id": intent_id,
                "symbol": symbol,
                "side": side,
                "body": body,
                "text": resp_text,
                "payload": intent_v1,
            },
        )
        return

    log.info(
        "broker_worker.intent.applied",
        extra={
            "status": resp.status_code,
            "intent_id": intent_id,
            "symbol": symbol,
            "side": side,
            "body": body,
        },
    )


# ---------------------------------------------------------------------------
# Intent loop
# ---------------------------------------------------------------------------

async def _intent_loop(instance: str, stop_event: asyncio.Event) -> None:
    channel = CHANNELS["order"]
    log.info("broker_worker.intent_loop.start", extra={"instance": instance, "channel": channel})

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
                log.exception("broker_worker.intent.unpack_error", extra={"error": repr(exc)})
                continue

            intent_raw, meta = _normalize_envelope(payload)
            if intent_raw is None or not isinstance(intent_raw, dict):
                log.error(
                    "broker_worker.intent.bad_payload_type",
                    extra={"payload_type": type(payload).__name__},
                )
                continue

            intent_v1 = _ensure_intent_v1(intent_raw, meta)

            log.info(
                "broker_worker.intent.rx",
                extra={
                    "instance": instance,
                    "intent_id": intent_v1.get("intent_id"),
                    "symbol": intent_v1.get("symbol"),
                    "side": intent_v1.get("side"),
                    "strategy_id": intent_v1.get("strategy_id"),
                    "strength": intent_v1.get("strength"),
                    "account_id": intent_v1.get("account_id"),
                },
            )

            await _forward_intent_to_trader(intent_v1)

    finally:
        with contextlib.suppress(Exception):
            await ps.unsubscribe(channel)
        with contextlib.suppress(Exception):
            await _ps_aclose(ps)
        log.info("broker_worker.intent_loop.stop", extra={"instance": instance, "channel": channel})


# ---------------------------------------------------------------------------
# Runner / signal handling (Windows-safe)
# ---------------------------------------------------------------------------

async def worker_main(instance: str, stop_event: Optional[asyncio.Event] = None) -> None:
    if stop_event is None:
        stop_event = asyncio.Event()

    log.info("broker_worker.start", extra={"instance": instance, "redis": get_redis_url()})

    try:
        await _intent_loop(instance, stop_event)
    except asyncio.CancelledError:
        log.info("broker_worker.cancelled", extra={"instance": instance})
    except Exception as exc:
        log.exception("broker_worker.error", extra={"instance": instance, "error": repr(exc)})
    finally:
        log.info("broker_worker.stop", extra={"instance": instance})


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        log.debug("broker_worker.signals.installed")
    except (NotImplementedError, RuntimeError):
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
        default=os.getenv("REFLEX__INSTANCE") or os.getenv("INSTANCE") or get_instance_id(),
        help="logical instance id for logging/metrics",
    )
    return p.parse_args(argv)


def _main() -> int:
    args = _parse_args(sys.argv[1:])
    instance = str(args.instance)

    log.info(
        "broker_worker.bootstrap",
        extra={
            "instance": instance,
            "redis_url": get_redis_url(),
            "trader_base": _trader_base_url(),
            "intent_path": _intent_endpoint_path(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        },
    )

    try:
        asyncio.run(_runner(instance))
        return 0
    except KeyboardInterrupt:
        log.info("broker_worker.keyboard_interrupt", extra={"instance": instance})
        return 0
    except Exception as exc:
        log.exception("broker_worker.crashed", extra={"instance": instance, "error": repr(exc)})
        return 1


if __name__ == "__main__" or __package__ == "trader.broker_worker":
    sys.exit(_main())
