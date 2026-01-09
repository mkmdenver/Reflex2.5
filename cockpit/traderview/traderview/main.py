"""TraderView FastAPI backend.

Separate operator console from BrokerView.

Scope (v0):
  - Serve the React SPA for Plan Monitor
  - Provide /v1/time (proxy Trader time) for clock sync
  - (Later) proxy Trader plan/runtime endpoints

Design: thin proxy; Trader remains source of truth.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("traderview")


def _env(name: str, default: Optional[str] = None) -> str:
    val = os.getenv(name, default)
    if val is None:
        raise RuntimeError(f"Missing required env var {name}")
    return val


TRADERVIEW_PORT = int(_env("TRADERVIEW_PORT", "7011"))
TRADER_API_PORT = int(_env("TRADER_API_PORT", "7002"))
TRADER_BASE = _env("TRADER_BASE", f"http://127.0.0.1:{TRADER_API_PORT}")

# ROOT for static/templates
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

log.info(
    "TraderView config port=%s trader_base=%s root=%s",
    TRADERVIEW_PORT,
    TRADER_BASE,
    ROOT,
)

app = FastAPI(title="TraderView")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static assets (built React bundle)
static_dir = os.path.join(ROOT, "cockpit", "traderview", "templates", "dist", "assets")
if os.path.isdir(static_dir):
    app.mount("/assets", StaticFiles(directory=static_dir), name="assets")


@app.get("/v1/time")
async def time_now() -> Dict[str, Any]:
    """Return authoritative time for UI sync.

    Truth chain:
      Trader (/v1/time) -> TraderView proxy -> UI offset

    If Trader is unavailable, we return local UTC but mark stale.
    """
    try:
        async with httpx.AsyncClient(timeout=5) as cli:
            r = await cli.get(f"{TRADER_BASE}/v1/time")
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and "source" not in data:
                data["source"] = "trader"
            return data
    except Exception as e:
        now = datetime.now(timezone.utc)
        return {
            "ok": True,
            "server_utc_ms": int(now.timestamp() * 1000),
            "server_iso": now.isoformat().replace("+00:00", "Z"),
            "source": "traderview_local_fallback",
            "stale": True,
            "error": repr(e),
        }


@app.get("/v1/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "service": "traderview", "trader_base": TRADER_BASE}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    index_html = os.path.join(ROOT, "cockpit", "traderview", "templates", "dist", "index.html")
    # When running dev mode before build, this file won't exist.
    # That's fine; users should run npm build to produce dist.
    if not os.path.exists(index_html):
        return (
            "<h3>TraderView UI not built.</h3>"
            "<p>Run <code>npm install</code> and <code>npm run build</code> in "
            "<code>cockpit/traderview/templates</code>.</p>"
        )
    with open(index_html, "r", encoding="utf-8") as f:
        return f.read()
