# =============================================================================
# ticks_ui.py — Flask Blueprint for Ticks→Parquet backfill GUI (popup console)
# Version: 2025.10.22-rc1
#
# CHANGELOG
# - 2025-10-22: rc1
#   • Compact form with start/end, mode, workers, force, lineage.
#   • Background thread runner with SSE log stream per job_id.
#   • Simple in-memory job registry (not persistent; good for local ops).
# =============================================================================
from __future__ import annotations
import os, threading, queue, uuid, datetime as dt, pathlib
from typing import Dict, Callable, Optional

from flask import Blueprint, render_template, request, Response, stream_with_context, current_app, jsonify

from ticks_backfill import BackfillPlan, run_backfill

bp = Blueprint("ticks_ui", __name__, template_folder="templates", static_folder="static", url_prefix="")

# ---- In-memory job registry --------------------------------------------------
_JOBS: Dict[str, "Job"] = {}

class Job:
    def __init__(self, dsn: str, api_key: str, parquet_root: pathlib.Path, start: dt.date, end: dt.date,
                 mode: str, workers: int, force: bool, lineage: bool):
        self.id = str(uuid.uuid4())
        self.q: "queue.Queue[str]" = queue.Queue()
        self.done = False
        self.err: Optional[str] = None
        self.thread = threading.Thread(target=self._run, daemon=True, args=(dsn, api_key, parquet_root, start, end, mode, workers, force, lineage))
        self.thread.start()

    def _hook(self, msg: str):
        try: self.q.put_nowait(msg)
        except Exception: pass

    def _run(self, dsn, api_key, parquet_root, start, end, mode, workers, force, lineage):
        try:
            plan = BackfillPlan(
                start=start, end=end, mode=mode, workers=workers, force=force,
                parquet_root=parquet_root, log_hook=self._hook
            )
            run_backfill(dsn, api_key, plan, lineage=lineage)
        except Exception as e:
            self.err = str(e)
            self._hook(f"[FATAL] {e}")
        finally:
            self.done = True
            self._hook("[CLOSE]")

# ---- Routes -----------------------------------------------------------------
@bp.route("/backfill/ticks", methods=["GET"])
def ticks_form():
    return render_template("ticks_backfill.html")

@bp.route("/backfill/ticks", methods=["POST"])
def ticks_start():
    dsn = os.getenv("DATABASE_URL", "")
    api_key = os.getenv("POLYGON_API_KEY", "")
    parquet_root = pathlib.Path(os.getenv("PARQUET_ROOT", "") or (os.path.expanduser("~/market")))

    start_s = (request.form.get("start") or "2021-01-01").strip()
    end_s   = (request.form.get("end") or "today").strip()
    mode    = (request.form.get("mode") or "candidates").strip()
    workers = int(request.form.get("workers") or "6")
    force   = bool(request.form.get("force"))
    lineage = bool(request.form.get("lineage"))

    if end_s.lower() == "today":
        end = dt.datetime.utcnow().date()
    else:
        end = dt.datetime.strptime(end_s, "%Y-%m-%d").date()
    start = dt.datetime.strptime(start_s, "%Y-%m-%d").date()

    job = Job(dsn, api_key, parquet_root, start, end, mode, workers, force, lineage)
    _JOBS[job.id] = job
    return jsonify({"job_id": job.id})

@bp.route("/events/backfill/<job_id>")
def backfill_events(job_id: str):
    job = _JOBS.get(job_id)
    if not job:
        return Response("not found", status=404)

    @stream_with_context
    def event_stream():
        yield "retry: 1000\n\n"
        while True:
            try:
                line = job.q.get(timeout=0.25)
                yield f"data: {line}\n\n"
            except queue.Empty:
                if job.done:
                    break
        yield "data: [CLOSE]\n\n"

    return Response(event_stream(), mimetype="text/event-stream")

@bp.route("/backfill/status/<job_id>")
def backfill_status(job_id: str):
    job = _JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "unknown job"})
    return jsonify({"ok": True, "done": job.done, "error": job.err})
