"""TraderView FastAPI backend (separate product).

Serves a static HTML UI and proxies plan APIs to Trader.
"""

from __future__ import annotations

import os
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

@app.get("/v1/plans/active")
async def plans_active(account_id: Optional[str] = None) -> Dict[str, Any]:
    params = {}
    if account_id:
        params["account_id"] = account_id
    async with httpx.AsyncClient(timeout=10) as cli:
        try:
            r = await cli.get(f"{TRADER_BASE}/v1/plans/active", params=params)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            log.exception("proxy.plans_active.failed %s", e)
            raise HTTPException(status_code=502, detail="Trader unavailable")

async def _proxy_post(path: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as cli:
        try:
            r = await cli.post(f"{TRADER_BASE}{path}", json={})
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
        except httpx.HTTPError as e:
            log.exception("proxy.post.failed %s", e)
            raise HTTPException(status_code=502, detail="Trader unavailable")

@app.post("/v1/plans/{plan_id}/ack")
async def plan_ack(plan_id: str) -> Dict[str, Any]:
    return await _proxy_post(f"/v1/plans/{plan_id}/ack")

@app.post("/v1/plans/{plan_id}/kill")
async def plan_kill(plan_id: str) -> Dict[str, Any]:
    return await _proxy_post(f"/v1/plans/{plan_id}/kill")

@app.post("/v1/plans/{plan_id}/flatten")
async def plan_flatten(plan_id: str) -> Dict[str, Any]:
    return await _proxy_post(f"/v1/plans/{plan_id}/flatten")
