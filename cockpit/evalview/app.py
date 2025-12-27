# cockpit/evalview/app.py
#
# Reflex EvalView — tiny dashboard for evaluator telemetry.
#
# Bots publish JSON snapshots to a Redis pub/sub channel; we keep an
# in-memory dict keyed by eval_id and render it via Jinja templates.

import os
import time
import json
import asyncio
from pathlib import Path
from typing import Dict, Any

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------
# Paths / templates / static
# ---------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(title="Reflex EvalView")

app.mount(
    "/static",
    StaticFiles(directory=str(STATIC_DIR)),
    name="static",
)

# ---------------------------------------------------------------------
# In-memory evaluator state
# ---------------------------------------------------------------------

EVAL_STATE_CHANNEL = os.getenv("EVAL_STATE_CHANNEL", "eval.state")
eval_instances: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------------
# Redis subscriber background task
# ---------------------------------------------------------------------

async def redis_listener() -> None:
    """
    Subscribe to the eval telemetry channel and keep an in-memory view of
    the latest snapshot for each eval_id.

    Uses pubsub.get_message() in a loop instead of the async generator
    interface to avoid aclose()/GeneratorExit weirdness on shutdown.
    """
    url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    print(f"[EvalView] Connecting to Redis at {url!r}")

    r = aioredis.from_url(url, decode_responses=True)
    pubsub = r.pubsub()

    try:
        await pubsub.subscribe(EVAL_STATE_CHANNEL)
        print(f"[EvalView] Listening on Redis channel: {EVAL_STATE_CHANNEL}")

        while True:
            # get_message returns immediately if something is waiting,
            # otherwise waits up to timeout seconds.
            msg = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=1.0,
            )
            if msg is None:
                # No message, just idle a bit
                await asyncio.sleep(0.1)
                continue

            raw = msg.get("data")
            try:
                data = json.loads(raw)
            except Exception as exc:
                print(f"[EvalView] JSON decode error: {exc!r} raw={raw!r}")
                continue

            eval_id = data.get("eval_id")
            if not eval_id:
                continue

            eval_instances[eval_id] = {
                "eval_id": eval_id,
                "modules": data.get("modules", []),
                "rows": data.get("rows", 0),
                "last": time.time(),
                "raw": data,
            }

            print(
                f"[EvalView] update eval_id={eval_id!r} "
                f"modules={data.get('modules')} "
                f"rows={data.get('rows')} "
                f"total_instances={len(eval_instances)}"
            )

    except asyncio.CancelledError:
        print("[EvalView] redis_listener cancelled, shutting down listener")
    except Exception as exc:
        print(f"[EvalView] redis_listener error: {exc!r}")
    finally:
        try:
            await pubsub.unsubscribe(EVAL_STATE_CHANNEL)
        except Exception:
            pass
        try:
            await pubsub.close()
        except Exception:
            pass
        try:
            await r.close()
        except Exception:
            pass
        print("[EvalView] redis_listener stopped")


@app.on_event("startup")
async def startup_event() -> None:
    # Fire and forget; if the task dies, the logs will tell us why.
    asyncio.create_task(redis_listener())


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------

@app.get("/", name="index", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    now = time.time()
    rows = []

    for e in eval_instances.values():
        age = now - e["last"]
        rows.append(
            {
                "eval_id": e["eval_id"],
                "modules": e["modules"],
                "rows": e["rows"],
                "age_sec": round(age, 1),
            }
        )

    rows.sort(key=lambda r: r["age_sec"])

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "evals": rows,
            "channel": EVAL_STATE_CHANNEL,
        },
    )


@app.get("/eval/{eval_id}", response_class=HTMLResponse)
def eval_view(request: Request, eval_id: str) -> HTMLResponse:
    e = eval_instances.get(eval_id)
    if not e:
        return templates.TemplateResponse(
            "eval_not_found.html",
            {
                "request": request,
                "eval_id": eval_id,
                "EVAL_STATE_CHANNEL": EVAL_STATE_CHANNEL,
            },
        )

    return templates.TemplateResponse(
        "eval_show.html",
        {
            "request": request,
            "data": e["raw"],
            "eval_id": eval_id,
            "age": round(time.time() - e["last"], 1),
        },
    )


@app.get("/debug/evals", response_class=JSONResponse)
def debug_evals() -> JSONResponse:
    now = time.time()
    out = []

    for e in eval_instances.values():
        out.append(
            {
                "eval_id": e["eval_id"],
                "modules": e["modules"],
                "rows": e["rows"],
                "age_sec": round(now - e["last"], 3),
            }
        )

    return JSONResponse(
        {
            "channel": EVAL_STATE_CHANNEL,
            "count": len(out),
            "evals": out,
        }
    )
