"""TraderView FastAPI backend (separate product).

Serves a static HTML UI and provides a thin status layer.

IMPORTANT (current reality):
- Trader does NOT expose /v1/plans/* endpoints yet.
- Therefore TraderView must NOT proxy /v1/plans/active to Trader.
- We return a stable stub response instead, and optionally use /v1/health
  (which Trader DOES expose) to show Trader online/offline.
"""

from __future__ import annotations

import os
import time
import logging
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException
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

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

app = FastAPI(title="TraderView")

assets_dir = os.path.join(ROOT, "cockpit", "traderview", "templates", "dist", "assets")
if os.path.isdir(assets_dir):
    app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    p = os.path.join(ROOT, "cockpit", "traderview", "templates", "dist", "index.html")
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


# --------------------------------------------------------------------------------------
# Time sync endpoint (used by UI MarketClock)
# UI expects: { server_utc_ms: number, server_iso?: string, source?: string }
# --------------------------------------------------------------------------------------
@app.get("/v1/time")
async def time_sync() -> Dict[str, Any]:
    # Keep it simple: server time (TraderView host) is enough for UI offset.
    utc_ms = int(time.time() * 1000)
    return {"server_utc_ms": utc_ms, "server_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "source": "traderview"}


# --------------------------------------------------------------------------------------
# Trader status (real endpoint that exists in Trader: GET /v1/health)
# If Trader is down/unreachable, we return ok=false but NOT a 502 (UI can display).
# --------------------------------------------------------------------------------------
@app.get("/v1/trader/status")
async def trader_status() -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=5) as cli:
        try:
            r = await cli.get(f"{TRADER_BASE}/v1/health")
            if r.status_code == 404:
                # Some builds may not include /v1/health; treat as "unknown but reachable"
                return {"ok": True, "reachable": True, "health": None, "note": "Trader reachable; /v1/health not found"}
            r.raise_for_status()
            return {"ok": True, "reachable": True, "health": r.json()}
        except Exception as e:
            log.warning("trader_status.failed %s", e)
            return {"ok": False, "reachable": False, "error": str(e)}


# --------------------------------------------------------------------------------------
# Plans API stubs (Trader does not have plans yet)
# Return a stable shape so the UI doesn't error.
# --------------------------------------------------------------------------------------
@app.get("/v1/plans/active")
async def plans_active(account_id: Optional[str] = None) -> Dict[str, Any]:
    # Stable doc shape for UI:
    # { ok: bool, plans: [], trader_ok: bool, note?: str }
    # Keep it empty until PlanManager lands in Trader.
    trader_ok = False
    note = "Plans not implemented in Trader yet (stub response from TraderView)."

    # Best-effort: if Trader health responds, mark trader_ok True.
    async with httpx.AsyncClient(timeout=3) as cli:
        try:
            r = await cli.get(f"{TRADER_BASE}/v1/health")
            if r.status_code in (200, 404):
                trader_ok = True
        except Exception:
            pass

    return {
        "ok": True,
        "plans": [],
        "trader_ok": trader_ok,
        "account_id": account_id,
        "note": note,
    }


@app.post("/v1/plans/{plan_id}/ack")
async def plan_ack(plan_id: str) -> Dict[str, Any]:
    raise HTTPException(status_code=501, detail="PlanManager not implemented in Trader yet")


@app.post("/v1/plans/{plan_id}/kill")
async def plan_kill(plan_id: str) -> Dict[str, Any]:
    raise HTTPException(status_code=501, detail="PlanManager not implemented in Trader yet")


@app.post("/v1/plans/{plan_id}/flatten")
async def plan_flatten(plan_id: str) -> Dict[str, Any]:
    raise HTTPException(status_code=501, detail="PlanManager not implemented in Trader yet")
