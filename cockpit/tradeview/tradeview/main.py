"""
TradeView FastAPI backend.

Sister cockpit product to BrokerView.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from glob import glob
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("tradeview")

def _env(name: str, default: Optional[str] = None) -> str:
    v = os.getenv(name, default)
    if v is None:
        raise RuntimeError(f"Missing required env var {name}")
    return v

PORT = int(_env("TRADEVIEW_PORT", "7012"))

TRADER_API_PORT = int(_env("TRADER_API_PORT", "7002"))
TRADER_BASE = _env("TRADER_BASE", f"http://127.0.0.1:{TRADER_API_PORT}")

DATAHUB_API_PORT = int(_env("DATAHUB_API_PORT", "7000"))
DATAHUB_BASE = _env("DATAHUB_BASE", f"http://127.0.0.1:{DATAHUB_API_PORT}")

# ROOT for static/templates
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LOGS_DIR = os.path.join(ROOT, "logs")

app = FastAPI(title="TradeView")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = os.path.join(ROOT, "cockpit", "tradeview", "templates", "dist", "assets")
if os.path.isdir(static_dir):
    app.mount("/assets", StaticFiles(directory=static_dir), name="assets")

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    index_html = os.path.join(ROOT, "cockpit", "tradeview", "templates", "dist", "index.html")
    with open(index_html, "r", encoding="utf-8") as f:
        return f.read()

def _now_ms() -> int:
    return int(time.time() * 1000)

def _tail_lines(path: str, max_lines: int = 800) -> List[str]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            block = 8192
            data = b""
            pos = end
            while pos > 0 and data.count(b"\n") <= max_lines:
                pos = max(0, pos - block)
                f.seek(pos)
                data = f.read(end - pos) + data
                end = pos
            lines = data.splitlines()[-max_lines:]
        return [ln.decode("utf-8", errors="replace") for ln in lines if ln.strip()]
    except Exception as e:
        log.warning("tail failed path=%s err=%r", path, e)
        return []

def _parse_jsonl(lines: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out

async def _ping(url: str, timeout: float = 1.2) -> Tuple[bool, Optional[float]]:
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as cli:
            r = await cli.get(url)
            ok = r.status_code == 200
            dt = (time.time() - t0) * 1000.0
            return ok, dt
    except Exception:
        return False, None

def _iso_from_ms(ms: Optional[int]) -> Optional[str]:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")

@app.get("/v1/summary")
async def summary() -> Dict[str, Any]:
    now = _now_ms()
"""
TradeView FastAPI backend.

Sister cockpit product to BrokerView.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from glob import glob
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("tradeview")

def _env(name: str, default: Optional[str] = None) -> str:
    v = os.getenv(name, default)
    if v is None:
        raise RuntimeError(f"Missing required env var {name}")
    return v

PORT = int(_env("TRADEVIEW_PORT", "7012"))

TRADER_API_PORT = int(_env("TRADER_API_PORT", "7002"))
TRADER_BASE = _env("TRADER_BASE", f"http://127.0.0.1:{TRADER_API_PORT}")

DATAHUB_API_PORT = int(_env("DATAHUB_API_PORT", "7000"))
DATAHUB_BASE = _env("DATAHUB_BASE", f"http://127.0.0.1:{DATAHUB_API_PORT}")

# ROOT for static/templates
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LOGS_DIR = os.path.join(ROOT, "logs")

app = FastAPI(title="TradeView")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = os.path.join(ROOT, "cockpit", "tradeview", "templates", "dist", "assets")
if os.path.isdir(static_dir):
    app.mount("/assets", StaticFiles(directory=static_dir), name="assets")

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    index_html = os.path.join(ROOT, "cockpit", "tradeview", "templates", "dist", "index.html")
    with open(index_html, "r", encoding="utf-8") as f:
        return f.read()

def _now_ms() -> int:
    return int(time.time() * 1000)

def _tail_lines(path: str, max_lines: int = 800) -> List[str]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            block = 8192
            data = b""
            pos = end
            while pos > 0 and data.count(b"\n") <= max_lines:
                pos = max(0, pos - block)
                f.seek(pos)
                data = f.read(end - pos) + data
                end = pos
            lines = data.splitlines()[-max_lines:]
        return [ln.decode("utf-8", errors="replace") for ln in lines if ln.strip()]
    except Exception as e:
        log.warning("tail failed path=%s err=%r", path, e)
        return []

def _parse_jsonl(lines: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out

async def _ping(url: str, timeout: float = 1.2) -> Tuple[bool, Optional[float]]:
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as cli:
            r = await cli.get(url)
            ok = r.status_code == 200
            dt = (time.time() - t0) * 1000.0
            return ok, dt
    except Exception:
        return False, None

def _iso_from_ms(ms: Optional[int]) -> Optional[str]:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")

@app.get("/v1/summary")
async def summary() -> Dict[str, Any]:
    now = _now_ms()

# MVP: active trades are the live positions from Trader.
items: List[Dict[str, Any]] = []
try:
    async with httpx.AsyncClient(timeout=2.5) as cli:
        r = await cli.get(f"{TRADER_BASE}/v1/portfolio/positions")
        r.raise_for_status()
        data = r.json()
        pos = data.get("items") if isinstance(data, dict) else None
        if isinstance(pos, list):
            for p in pos:
                items.append({
                    "symbol": p.get("symbol"),
                    "state": "ACTIVE",
                    "side": p.get("side"),
                    "qty": p.get("qty"),
                    "entry": p.get("avg_entry_price") or p.get("entry_price"),
                    "stop": p.get("stop_price"),
                    "target": p.get("target_price"),
                    "unrealized_pl": p.get("unrealized_pl"),
                    "pti": p.get("gen_id") or p.get("pti") or "—",
                })
except Exception as e:
    return {"ok": False, "error": "trader_unavailable", "detail": repr(e), "items": items}

return {"ok": True, "now_ms": now, "items": items}
