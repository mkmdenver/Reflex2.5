# trader/broker_worker.py
# Version: 2026-01-12
# Purpose:
#   Bridge Evaluator intents on Redis -> Trader HTTP /v1/intents (standard path).
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
#   • qty/shares are NOT required here. Trader sizes (Risk/Capital) and compiles orders.
#
# Critical safety rules (enforced here):
#   - Instance-scoped channels are mandatory:
#       eval.order_intent.live
#       eval.order_intent.replay
#   - If payload declares origin instance_id and it doesn't match this worker instance -> reject
#   - If this Trader is LIVE and payload declares REPLAY -> reject
#
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

import json
from datetime import datetime, timezone

TRADER_EVENT_LOG_ENABLE = (os.getenv("TRADER_EVENT_LOG_ENABLE") or "").lower() in ("1","true","yes","on")
TRADER_EVENT_LOG_PATH = (os.getenv("TRADER_EVENT_LOG_PATH") or "logs/trader_events.jsonl").strip()

def _ts_iso(ts: float | None = None) -> str:
    t = ts if ts is not None else time.time()
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

def _append_trader_event(evt: Dict[str, Any]) -> None:
    if not TRADER_EVENT_LOG_ENABLE:
        return
    try:
        path = TRADER_EVENT_LOG_PATH
        os.makedirs(os.path.dirname(path), exist_ok=True)
        now = time.time()
        evt.setdefault("ts", now)
        evt.setdefault("ts_iso", _ts_iso(now))
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(evt, separators=(",", ":"), default=str) + "\n")
    except Exception:
        # NEVER break trading for logging
        pass

# ---------------------------------------------------------------------------
# Channel resolution + safety gates
# ---------------------------------------------------------------------------

def _normalize_instance_id(instance: str) -> str:
    """
    Standardize instance ids:
      - live (or 'live') -> live
      - replay (or 'replay') -> replay
    """
    inst = (instance or "").strip()
    low = inst.lower()
    if low in ("live", "live"):
        return "live"
    if low in ("replay", "replay"):
        return "replay"
    return inst


def _resolve_channel(instance: str, *, env_keys: list[str], default_base: str) -> str:
    """
    Resolve a Redis pubsub channel name with strict instance scoping.

    - Reads the first non-empty env var in env_keys.
    - Supports '{instance_id}' placeholder formatting.
    - If the channel equals the bare base name (e.g. 'eval.intent'), it is automatically
      expanded to 'eval.intent.<instance>'.
    - Back-compat: if the channel equals 'eval.order_intent', it expands to 'eval.order_intent.<instance>'.

    This keeps LIVE and REPLAY universes from colliding.
    """
    ch = ""
    for k in env_keys:
        v = (os.getenv(k) or "").strip()
        if v:
            ch = v
            break

    if not ch:
        ch = default_base

    if "{instance_id}" in ch:
        ch = ch.format(instance_id=instance)

    # hard rule: bare base must be instance-scoped
    if ch in (default_base, "eval.order_intent"):
        ch = f"{ch}.{instance}"

    return ch


def _resolve_bot_intent_channel(instance: str) -> str:
    # Prefer explicit new names, then legacy fallbacks.
    return _resolve_channel(
        instance,
        env_keys=["BOT_INTENT_CHANNEL", "PTI_INTENT_CHANNEL", "INTENT_CHANNEL", "ORDER_CHANNEL"],
        default_base="eval.intent",
    )


def _resolve_manual_intent_channel(instance: str) -> str:
    return _resolve_channel(
        instance,
        env_keys=["MANUAL_INTENT_CHANNEL"],
        default_base="manual.intent",
    )

def _payload_instance(meta: Dict[str, Any], intent: Dict[str, Any]) -> Optional[str]:
    """Try to extract an originating instance id from meta/intent (best-effort)."""
    for k in ("instance_id", "instance", "reflex_instance_id", "source_instance"):
        v = meta.get(k) if isinstance(meta, dict) else None
        if isinstance(v, str) and v.strip():
            return v.strip()
    for k in ("instance_id", "reflex_instance_id"):
        v = intent.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _payload_mode(meta: Dict[str, Any], intent: Dict[str, Any]) -> Optional[str]:
    """Try to extract mode (LIVE/REPLAY) from meta/intent (best-effort)."""
    for k in ("mode", "reflex_mode", "feed_mode"):
        v = meta.get(k) if isinstance(meta, dict) else None
        if isinstance(v, str) and v.strip():
            return v.strip().upper()
    for k in ("mode", "reflex_mode", "feed_mode"):
        v = intent.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().upper()
    return None


def _should_accept_message(*, this_instance: str, this_mode: str, intent: Dict[str, Any], meta: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Safety gate to prevent cross-stream mistakes.
    - If message declares an instance_id and it doesn't match this instance -> reject
    - If this Trader is LIVE and message declares REPLAY -> reject
    """
    origin_instance = _payload_instance(meta, intent)
    if origin_instance:
        origin_instance = _normalize_instance_id(origin_instance)
        if origin_instance != this_instance:
            return False, f"instance_mismatch origin={origin_instance} this={this_instance}"

    origin_mode = _payload_mode(meta, intent)
    if this_mode == "LIVE" and origin_mode == "REPLAY":
        return False, "live_rejects_replay"

    return True, "ok"


# ---------------------------------------------------------------------------
# Payload normalization
# ---------------------------------------------------------------------------

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
    Convert a legacy order-ish message into an Intent v1-ish message.
    This keeps replay/live ingestion compatible while Trader remains the authority.
    """
    out: Dict[str, Any] = {}

    out["intent_id"] = intent.get("intent_id") or f"legacy:{int(time.time()*1000)}"
    out["ts"] = intent.get("ts") or time.time()
    out["symbol"] = intent.get("symbol")
    out["side"] = intent.get("side")
    out["strategy_id"] = intent.get("strategy_id") or intent.get("strategy") or "legacy"
    out["source"] = intent.get("source") or "legacy"
    out["reason"] = intent.get("reason") or intent.get("note") or ""

    # Legacy qty/shares may exist but Trader can ignore/recompute sizing.
    if "shares" in intent:
        out["shares"] = intent.get("shares")
    elif "qty" in intent:
        out["shares"] = intent.get("qty")

    # Pass through any account hint
    if intent.get("account_id"):
        out["account_id"] = intent.get("account_id")

    # Carry risk hints if present
    if isinstance(intent.get("risk_hints"), dict):
        out["risk_hints"] = intent.get("risk_hints")

    # attach legacy hints into meta if needed
    legacy_hints: Dict[str, Any] = {}
    if intent.get("type"):
        legacy_hints["order_type_legacy"] = intent.get("type")
    if intent.get("time_in_force") or intent.get("tif"):
        legacy_hints["tif_legacy"] = intent.get("time_in_force") or intent.get("tif")
    if "extended_hours" in intent:
        legacy_hints["extended_hours_legacy"] = bool(intent.get("extended_hours"))

    if legacy_hints:
        meta = dict(meta or {})
        meta.setdefault("legacy", {})
        if isinstance(meta["legacy"], dict):
            meta["legacy"].update(legacy_hints)

    return out


def _ensure_intent_v1(intent: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure outgoing payload is Intent v1.
    - If legacy order-ish, convert.
    - If already v1-ish, keep.
    """
    if ("qty" in intent) or ("time_in_force" in intent) or ("tif" in intent) or ("type" in intent):
        return _legacy_to_intent_v1(intent, meta)
    return intent


# ---------------------------------------------------------------------------
# Trader forwarding
# ---------------------------------------------------------------------------

def _trader_base_url() -> str:
    port = get_trader_api_port()
    return f"http://127.0.0.1:{port}"


def _intent_endpoint_path() -> str:
    # keep stable: Trader API accepts /v1/intents
    return "/v1/intents"


async def _forward_intent_to_trader(intent_v1: Dict[str, Any]) -> None:
    base = _trader_base_url()
    url = base + _intent_endpoint_path()

    intent_id = intent_v1.get("intent_id")
    symbol = intent_v1.get("symbol")
    side = intent_v1.get("side")

    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=intent_v1)

    resp_text: str = ""
    with contextlib.suppress(Exception):
        resp_text = resp.text

    body: Any = None
    with contextlib.suppress(Exception):
        body = resp.json()

    if resp.status_code != 200:
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
        _append_trader_event({
            "kind": "intent.apply_error",
            "intent_id": intent_id,
            "symbol": symbol,
            "side": side,
            "status": resp.status_code,
            "body": body,
        })

        return

    log.info(
        "broker_worker.intent.applied",
        extra={
            "intent_id": intent_id,
            "symbol": symbol,
            "side": side,
            "body": body,
        },
    )
    _append_trader_event({
        "kind": "intent.applied",
        "intent_id": intent_id,
        "symbol": symbol,
        "side": side,
        "response": body,
    })


# ---------------------------------------------------------------------------
# Intent loop
# ---------------------------------------------------------------------------

async def _ps_aclose(ps: Any) -> None:
    with contextlib.suppress(Exception):
        await ps.aclose()  # type: ignore[attr-defined]


async def _intent_loop(*, instance: str, channel: str, source: str, stop_event: asyncio.Event) -> None:
    log.info("broker_worker.intent_loop.start", extra={"instance": instance, "channel": channel, "source": source})
    print(f"Starting intent loop for instance {instance} ({source}) on channel {channel}")

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
            if not intent_raw:
                log.warning("broker_worker.intent.bad_payload", extra={"payload": payload})
                continue

            intent_v1 = _ensure_intent_v1(intent_raw, meta)

            # Safety gate: prevent cross-instance / cross-mode leakage
            this_mode = (os.getenv("REFLEX_MODE") or "LIVE").strip().upper()
            ok, why = _should_accept_message(
                this_instance=instance,
                this_mode=this_mode,
                intent=intent_v1,
                meta=meta,
            )
            if not ok:
                log.warning(
                    "broker_worker.intent.rejected",
                    extra={
                        "instance": instance,
                        "reason": why,
                        "intent_id": intent_v1.get("intent_id"),
                        "symbol": intent_v1.get("symbol"),
                        "side": intent_v1.get("side"),
                    },
                )
                _append_trader_event({
                    "kind": "intent.rejected",
                    "instance": instance,
                    "channel": channel,
                    "intent_id": intent_v1.get("intent_id"),
                    "symbol": intent_v1.get("symbol"),
                    "side": intent_v1.get("side"),
                    "reason": why,
                })

                continue

            _append_trader_event({
                "kind": "intent.received",
                "instance": instance,
                "channel": channel,
                "intent_id": intent_v1.get("intent_id"),
                "symbol": intent_v1.get("symbol"),
                "side": intent_v1.get("side"),
                "strategy_id": intent_v1.get("strategy_id"),
                "strength": intent_v1.get("strength"),
                "account_id": intent_v1.get("account_id"),
            })

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

            _append_trader_event({
                "kind": "intent.forward",
                "instance": instance,
                "intent_id": intent_v1.get("intent_id"),
                "symbol": intent_v1.get("symbol"),
                "side": intent_v1.get("side"),
                "endpoint": _intent_endpoint_path(),
            })


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

def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        log.debug("broker_worker.signals.installed")
    except (NotImplementedError, RuntimeError):
        log.debug("broker_worker.signals.unavailable")


async def worker_main(instance: str, stop_event: Optional[asyncio.Event] = None) -> None:
    if stop_event is None:
        stop_event = asyncio.Event()

    log.info("broker_worker.start", extra={"instance": instance, "redis": get_redis_url()})

    try:
        bot_ch = _resolve_bot_intent_channel(instance)
        manual_ch = _resolve_manual_intent_channel(instance)

        tasks = [
            asyncio.create_task(_intent_loop(instance=instance, channel=bot_ch, source="bot", stop_event=stop_event)),
            asyncio.create_task(_intent_loop(instance=instance, channel=manual_ch, source="manual", stop_event=stop_event)),
        ]
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        log.info("broker_worker.cancelled", extra={"instance": instance})
    except Exception as exc:
        log.exception("broker_worker.error", extra={"instance": instance, "error": repr(exc)})
    finally:
        log.info("broker_worker.stop", extra={"instance": instance})


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
    instance = _normalize_instance_id(str(args.instance))
    os.environ["REFLEX_INSTANCE_ID"] = instance  # keep helpers consistent

    log.info(
        "broker_worker.bootstrap",
        extra={
            "instance": instance,
            "redis_url": get_redis_url(),
            "trader_base": _trader_base_url(),
            "intent_path": _intent_endpoint_path(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "order_channel": os.getenv("ORDER_CHANNEL") or "",
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
