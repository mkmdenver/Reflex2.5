"""
EngineerView FastAPI backend.

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

log = logging.getLogger("engineerview")

def _env(name: str, default: Optional[str] = None) -> str:
    v = os.getenv(name, default)
    if v is None:
        raise RuntimeError(f"Missing required env var {name}")
    return v

PORT = int(_env("ENGINEERVIEW_PORT", "7013"))

TRADER_API_PORT = int(_env("TRADER_API_PORT", "7002"))
TRADER_BASE = _env("TRADER_BASE", f"http://127.0.0.1:{TRADER_API_PORT}")

DATAHUB_API_PORT = int(_env("DATAHUB_API_PORT", "7000"))
DATAHUB_BASE = _env("DATAHUB_BASE", f"http://127.0.0.1:{DATAHUB_API_PORT}")

# ROOT for static/templates
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LOGS_DIR = os.path.join(ROOT, "logs")

app = FastAPI(title="EngineerView")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = os.path.join(ROOT, "cockpit", "engineerview", "templates", "dist", "assets")
if os.path.isdir(static_dir):
    app.mount("/assets", StaticFiles(directory=static_dir), name="assets")

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    index_html = os.path.join(ROOT, "cockpit", "engineerview", "templates", "dist", "index.html")
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
EngineerView FastAPI backend.

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

log = logging.getLogger("engineerview")

def _env(name: str, default: Optional[str] = None) -> str:
    v = os.getenv(name, default)
    if v is None:
        raise RuntimeError(f"Missing required env var {name}")
    return v

PORT = int(_env("ENGINEERVIEW_PORT", "7013"))

TRADER_API_PORT = int(_env("TRADER_API_PORT", "7002"))
TRADER_BASE = _env("TRADER_BASE", f"http://127.0.0.1:{TRADER_API_PORT}")

DATAHUB_API_PORT = int(_env("DATAHUB_API_PORT", "7000"))
DATAHUB_BASE = _env("DATAHUB_BASE", f"http://127.0.0.1:{DATAHUB_API_PORT}")

# ROOT for static/templates
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LOGS_DIR = os.path.join(ROOT, "logs")

app = FastAPI(title="EngineerView")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = os.path.join(ROOT, "cockpit", "engineerview", "templates", "dist", "assets")
if os.path.isdir(static_dir):
    app.mount("/assets", StaticFiles(directory=static_dir), name="assets")

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    index_html = os.path.join(ROOT, "cockpit", "engineerview", "templates", "dist", "index.html")
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

trader_ok, trader_ms = await _ping(f"{TRADER_BASE}/v1/health")
datahub_ok, datahub_ms = await _ping(f"{DATAHUB_BASE}/v1/health")

# Log freshness: use mtimes of key logs if present.
def _mtime(path: str) -> Optional[int]:
    try:
        return int(os.path.getmtime(path) * 1000)
    except Exception:
        return None

# pick newest matching file for each family
def _newest(glob_pat: str) -> Tuple[Optional[str], Optional[int]]:
    best_p = None
    best_m = None
    for p in glob(glob_pat):
        m = _mtime(p)
        if m is None:
            continue
        if best_m is None or m > best_m:
            best_m = m
            best_p = p
    return best_p, best_m

fts_p, fts_m = _newest(os.path.join(LOGS_DIR, "fts_*_events.jsonl"))
pti_p, pti_m = _newest(os.path.join(LOGS_DIR, "pti_*_events.jsonl"))
trd_p, trd_m = _newest(os.path.join(LOGS_DIR, "trader_events.jsonl"))

# freshness_score: 100 if all seen in last 5s, 0 if >60s old
ages = []
for m in [fts_m, pti_m, trd_m]:
    if m:
        ages.append(max(0, now - m))
worst = max(ages) if ages else None
score = None
ok_logs = True
if worst is None:
    score = 0
    ok_logs = False
else:
    score = int(max(0, min(100, 100 - (worst / 60_000) * 100)))
    ok_logs = worst <= 30_000

status = {
    "trader": {"ok": trader_ok, "latency_ms": trader_ms},
    "datahub": {"ok": datahub_ok, "latency_ms": datahub_ms},
    "logs": {
        "ok": ok_logs,
        "freshness_score": score,
        "fts_last": _iso_from_ms(fts_m),
        "pti_last": _iso_from_ms(pti_m),
        "trader_last": _iso_from_ms(trd_m),
        "paths": {"fts": fts_p, "pti": pti_p, "trader": trd_p},
    },
}
return {"ok": True, "now_ms": now, "status": status}
