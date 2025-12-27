# evaluator/state_publisher.py
from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, Optional

import redis  # sync Redis client

from common import logging as log

COMPONENT = "eval.state_pub"


def _garnet_url() -> str:
    """
    Match the rest of Reflex (ipc_bus): prefer REFLEX__GARNET_URL, then GARNET_URL,
    then fall back to localhost DB 0.
    """
    return (
        os.environ.get("REFLEX__GARNET_URL")
        or os.environ.get("GARNET_URL")
        or "redis://127.0.0.1:6379/0"
    )


EVAL_STATE_CHANNEL = os.getenv("EVAL_STATE_CHANNEL", "eval.state")


@dataclass
class EvalState:
    """
    Snapshot of evaluator state for a single (eval_id, module, symbol).

    This is what cockpit/evalview will consume and render.
    """
    eval_id: str
    symbol: str
    module: str
    phase: str               # e.g. "filter", "pattern", "intent", "heartbeat"
    validity: float          # 0.0–1.0 “is this signal trustworthy right now?”
    risk: float              # arbitrary risk score, >0 means “hotter”
    edge: float              # arbitrary edge score, >0 means “favorable”
    metrics: Dict[str, Any] = field(default_factory=dict)

    ts_ns: int = 0
    host: str = ""
    pid: int = 0

    # optional human context
    note: Optional[str] = None

    def enrich(self) -> "EvalState":
        if not self.ts_ns:
            self.ts_ns = time.time_ns()
        if not self.host:
            self.host = socket.gethostname()
        if not self.pid:
            try:
                import os as _os
                self.pid = _os.getpid()
            except Exception:
                self.pid = 0
        return self


# cached Redis client
_client: Optional[redis.Redis] = None
_client_logged_ok = False
_client_logged_fail = False


def _get_client() -> Optional[redis.Redis]:
    global _client, _client_logged_ok, _client_logged_fail
    if _client is not None:
        return _client

    url = _garnet_url()
    try:
        _client = redis.Redis.from_url(
            url,
            decode_responses=False,
            socket_timeout=2,
            socket_connect_timeout=2,
            health_check_interval=30,
        )
        # lightweight ping to fail fast if Garnet is dead
        _client.ping()
        if not _client_logged_ok:
            log.info(COMPONENT, "client.ok", url=url)
            _client_logged_ok = True
        return _client
    except Exception as exc:
        if not _client_logged_fail:
            log.error(COMPONENT, "client.error", url=url, error=str(exc))
            _client_logged_fail = True
        return None


def _publish_json(channel: str, payload: Dict[str, Any]) -> None:
    """
    Publish a JSON payload to Garnet/Redis.

    Telemetry must never raise; errors are silently ignored,
    but we log the first failure for visibility.
    """
    r = _get_client()
    if r is None:
        return

    try:
        msg = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        subs = r.publish(channel, msg)
        log.debug(
            COMPONENT,
            "publish.ok",
            channel=channel,
            subs=subs,
        )
    except Exception as exc:
        log.error(COMPONENT, "publish.error", channel=channel, error=str(exc))
        # do not re-raise


class EvalStatePublisher:
    """
    Tiny helper to push structured evaluator state onto Garnet/Redis.

    Wire this into filters, pattern matchers, orchestrators, etc.
    Cockpit will subscribe to EVAL_STATE_CHANNEL and build “barber poles”
    from these rows.
    """

    def __init__(self, eval_id: Optional[str] = None, module: str = "core"):
        self.eval_id = eval_id or os.getenv("EVAL_ID") or os.getenv("EVAL_INSTANCE") or "evalA"
        self.module = module

    def publish_state(
        self,
        symbol: str,
        phase: str,
        validity: float,
        risk: float,
        edge: float,
        metrics: Optional[Dict[str, Any]] = None,
        note: Optional[str] = None,
        module: Optional[str] = None,
    ) -> None:
        """
        Fire-and-forget telemetry.

        Failures are swallowed; this should never block the eval loop.
        """
        try:
            st = EvalState(
                eval_id=self.eval_id,
                symbol=symbol.upper(),
                module=module or self.module,
                phase=phase,
                validity=float(validity),
                risk=float(risk),
                edge=float(edge),
                metrics=dict(metrics or {}),
                note=note,
            ).enrich()
            payload = asdict(st)
            _publish_json(EVAL_STATE_CHANNEL, payload)
        except Exception:
            return

    def heartbeat(self, phase: str = "heartbeat") -> None:
        """
        Emit a lightweight “I’m alive” row with no symbol.

        Cockpit can treat symbol="" as instance-level status.
        """
        self.publish_state(
            symbol="",
            phase=phase,
            validity=1.0,
            risk=0.0,
            edge=0.0,
            metrics={"kind": "heartbeat"},
            module=self.module,
            note="eval heartbeat",
        )
