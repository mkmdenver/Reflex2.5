# datahub/api.py
#
# DataHub external API:
#   - /health: basic liveness + env info (no DB)
#   - POST /v1/tiers/service: request a tier change (publishes to CHANNELS["raise"])
#
# This runs as a separate process from datahub.worker.
# It is a convenience surface for cockpit & tools.
#
# IMPORTANT:
#   - This does NOT read or write symbol_metadata.
#   - Service tier state is owned in-memory by datahub.worker.
#   - Symbol Manager is the only thing responsible for DB writes.

from __future__ import annotations

import os
import socket
import time

from flask import Flask, jsonify, request

from common.bus import CHANNELS, publish_sync

flask_app = Flask(__name__)


@flask_app.get("/health")
def health():
    """
    Basic liveness for this API process.
    For detailed DataHub metrics, hit worker's /internal/health.
    """
    host = socket.gethostname()
    now_ts = int(time.time())

    instance = os.getenv("REFLEX__INSTANCE_ID", "hubA")
    mode = os.getenv("REFLEX__HUB_MODE", "UNKNOWN")

    return jsonify({
        "instance": instance,
        "mode": mode,
        "host": host,
        "time": now_ts,
        "ok": True,
        "note": "For DataHub engine stats, call worker /internal/health.",
    })


@flask_app.post("/v1/tiers/service")
def set_service_tier():
    """
    Request a service-tier change for a symbol.

    JSON body:
      {
        "symbol": "ASNS",
        "tier": "HOT",
        "source": "engineer_panel"   # optional
      }

    Behavior:
      - Does NOT touch the DB.
      - Publishes a raise event to CHANNELS["raise"].
      - datahub.worker.TierRequestListener will:
          * update its IN-MEMORY tier map
          * emit a backfill request (minute+tick, scope=today) if this is a true raise.
    """
    data = request.get_json(force=True) or {}
    sym = (data.get("symbol") or "").upper()
    tier = (data.get("tier") or "").upper()
    src = data.get("source") or "http_api"

    if not sym or tier not in ("COLD", "WATCH", "WARM", "HOT"):
        return jsonify({
            "ok": False,
            "error": "invalid symbol or tier; expected tier in [COLD, WATCH, WARM, HOT]",
        }), 400

    ev = {
        "symbol": sym,
        "tier": tier,
        "source": src,
        "ts": time.time(),
    }

    publish_sync(CHANNELS["raise"], ev)

    return jsonify({"ok": True, "symbol": sym, "tier": tier, "source": src})


def create_app(state: object | None = None, **_ignored):
    return flask_app


if __name__ == "__main__":
    port = int(os.getenv("DATAHUB_API_PORT", os.getenv("REFLEX__SERVER__PORT", "7000")))
    flask_app.run(host="0.0.0.0", port=port, debug=False)
