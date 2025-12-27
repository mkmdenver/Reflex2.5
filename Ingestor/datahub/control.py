# datahub/control.py
from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, Optional

from common.utils import load_env
from common import logging as log
from common.comm_garnet import garnet_rcli  # same import style as other modules

from .registry import Registry
from .models import SymbolState, SymbolFlags

COMPONENT = __name__  # e.g., "datahub.control"


def _ns_now() -> int:
    try:
        return time.time_ns()
    except AttributeError:
        return int(time.time() * 1_000_000_000)


def _parse_state(s: str) -> Optional[SymbolState]:
    if not s:
        return None
    try:
        return SymbolState[s.upper()]
    except Exception:
        return None


def start_control_loop(registry: Registry) -> threading.Thread:
    """
    Spawn a background thread to consume evaluator control updates.

    Subscriptions:
      - ctrl.state_updates
      - ctrl.flag_updates

    Acks are published to:
      - ctrl.acks
    """
    env = load_env()
    url = env.get("GARNET_URL", "redis://127.0.0.1:6379")

    bus = garnet_rcli(url)     # publisher + psubscribe client
    acker = garnet_rcli(url)   # separate lightweight publisher

    p = bus.pubsub(ignore_subscribe_messages=True)
    p.psubscribe("ctrl.state_updates", "ctrl.flag_updates")

    log.info(COMPONENT, "control_loop_subscribed",
             channels="ctrl.state_updates, ctrl.flag_updates", url=url)

    def _apply_state(msg: Dict[str, Any]) -> None:
        sym = (msg.get("symbol") or "").upper()
        new_state = _parse_state(str(msg.get("new_state", "")))
        reason = msg.get("reason")
        if not sym or new_state is None:
            return
        registry.set_state(sym, new_state)
        acker.publish("ctrl.acks", json.dumps({
            "ok": True, "type": "state",
            "symbol": sym, "applied_state": new_state.name,
            "ts": _ns_now(), "reason": reason
        }))

    def _apply_flags(msg: Dict[str, Any]) -> None:
        sym = (msg.get("symbol") or "").upper()
        flags = msg.get("flags") or {}
        if not sym:
            return
        sf = SymbolFlags(
            no_trade=bool(flags.get("no_trade", False)),
            halted=bool(flags.get("halted", False)),
            restricted=bool(flags.get("restricted", False)),
            custom={k: v for k, v in flags.items()
                    if k not in ("no_trade", "halted", "restricted")}
        )
        registry.set_flags(sym, sf)
        acker.publish("ctrl.acks", json.dumps({
            "ok": True, "type": "flags",
            "symbol": sym,
            "applied_flags": {
                "no_trade": sf.no_trade,
                "halted": sf.halted,
                "restricted": sf.restricted,
                **sf.custom
            },
            "ts": _ns_now()
        }))

    def _loop() -> None:
        log.info(COMPONENT, "control_loop_started")
        for raw in p.listen():
            try:
                t = raw.get("type")
                if t not in ("message", "pmessage"):
                    continue

                ch = raw.get("channel") or raw.get("pattern") or ""
                if isinstance(ch, bytes):
                    ch = ch.decode("utf-8", "ignore")

                data = raw.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8", "ignore")

                try:
                    obj = json.loads(data)
                except Exception:
                    # Malformed payload; skip quietly
                    continue

                if ch == "ctrl.state_updates":
                    _apply_state(obj)
                elif ch == "ctrl.flag_updates":
                    _apply_flags(obj)
            except Exception as e:
                # Keep the loop resilient; log and continue
                log.warn(COMPONENT, "control_loop_error_continuing", err=str(e))

    t = threading.Thread(target=_loop, name="control_loop", daemon=True)
    t.start()
    return t
