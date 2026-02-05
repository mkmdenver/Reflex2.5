"""
ModelView FastAPI backend.

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

log = logging.getLogger("modelview")

def _env(name: str, default: Optional[str] = None) -> str:
    v = os.getenv(name, default)
    if v is None:
        raise RuntimeError(f"Missing required env var {name}")
    return v

PORT = int(_env("MODELVIEW_PORT", "7011"))

TRADER_API_PORT = int(_env("TRADER_API_PORT", "7002"))
TRADER_BASE = _env("TRADER_BASE", f"http://127.0.0.1:{TRADER_API_PORT}")

DATAHUB_API_PORT = int(_env("DATAHUB_API_PORT", "7000"))
DATAHUB_BASE = _env("DATAHUB_BASE", f"http://127.0.0.1:{DATAHUB_API_PORT}")

# ROOT for static/templates
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LOGS_DIR = os.path.join(ROOT, "logs")

app = FastAPI(title="ModelView")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = os.path.join(ROOT, "cockpit", "modelview", "templates", "dist", "assets")
if os.path.isdir(static_dir):
    app.mount("/assets", StaticFiles(directory=static_dir), name="assets")

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    index_html = os.path.join(ROOT, "cockpit", "modelview", "templates", "dist", "index.html")
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
ModelView FastAPI backend.

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

log = logging.getLogger("modelview")

def _env(name: str, default: Optional[str] = None) -> str:
    v = os.getenv(name, default)
    if v is None:
        raise RuntimeError(f"Missing required env var {name}")
    return v

PORT = int(_env("MODELVIEW_PORT", "7011"))

TRADER_API_PORT = int(_env("TRADER_API_PORT", "7002"))
TRADER_BASE = _env("TRADER_BASE", f"http://127.0.0.1:{TRADER_API_PORT}")

DATAHUB_API_PORT = int(_env("DATAHUB_API_PORT", "7000"))
DATAHUB_BASE = _env("DATAHUB_BASE", f"http://127.0.0.1:{DATAHUB_API_PORT}")

# ROOT for static/templates
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LOGS_DIR = os.path.join(ROOT, "logs")

app = FastAPI(title="ModelView")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = os.path.join(ROOT, "cockpit", "modelview", "templates", "dist", "assets")
if os.path.isdir(static_dir):
    app.mount("/assets", StaticFiles(directory=static_dir), name="assets")

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    index_html = os.path.join(ROOT, "cockpit", "modelview", "templates", "dist", "index.html")
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

# Discover model logs and compute simple activity metrics.
items: List[Dict[str, Any]] = []

# Patterns: fts_*_events.jsonl, pti_*_events.jsonl
patterns = [
    ("fts", os.path.join(LOGS_DIR, "fts_*_events.jsonl")),
    ("pti", os.path.join(LOGS_DIR, "pti_*_events.jsonl")),
]

for kind, pat in patterns:
    for path in glob(pat):
        name = os.path.splitext(os.path.basename(path))[0]
        lines = _tail_lines(path, 1200)
        evs = _parse_jsonl(lines)
        last_ts_ms: Optional[int] = None
        e60 = 0
        e5m = 0
        err5 = 0
        for e in reversed(evs):
            ts = e.get("ts")
            if isinstance(ts, (int, float)):
                last_ts_ms = int(ts * 1000)
                break
        for e in evs:
            ts = e.get("ts")
            if not isinstance(ts, (int, float)):
                continue
            age_ms = now - int(ts * 1000)
            if age_ms <= 60_000:
                e60 += 1
            if age_ms <= 300_000:
                e5m += 1
                lvl = str(e.get("level","")).upper()
                msg = str(e.get("msg",""))
                if "ERROR" in lvl or "exception" in msg.lower():
                    err5 += 1

        items.append({
            "kind": kind,
            "name": name,
            "instance_id": e.get("instance_id") if evs else None,
            "last_ts_ms": last_ts_ms,
            "last_iso": _iso_from_ms(last_ts_ms),
            "events_60s": e60,
            "events_5m": e5m,
            "errors_5m": err5,
            "path": path,
        })

# Stable ordering: newest first
items.sort(key=lambda x: x.get("last_ts_ms") or 0, reverse=True)
return {"ok": True, "now_ms": now, "items": items}
