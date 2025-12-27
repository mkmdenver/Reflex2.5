# common/metrics_http.py
from __future__ import annotations

import threading
import socket
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Optional, Union

# Optional lightweight logger; falls back to print if unavailable
try:
    from . import logging as clog
    def _log(level: str, msg: str, **kv):
        comp = "common.metrics_http"
        getattr(clog, level)(comp, msg, **kv)
except Exception:  # pragma: no cover
    def _log(level: str, msg: str, **kv):  # very quiet fallback
        if level in ("error", "warn"):
            print(f"[metrics_http:{level}] {msg} {kv}")

# Registry export (project uses this as a tiny global)
try:
    from .metrics import GLOBAL_METRICS as M  # noqa: F401
except Exception:  # pragma: no cover
    class _Noop:
        def scrape(self) -> str: return "app_uptime_seconds 0\n"
        # Minimal counters API in case callers use M.inc/set_gauge
        def inc(self, *_args, **_kw): pass
        def set_gauge(self, *_args, **_kw): pass
        def get_counter(self, *_args, **_kw): return 0
    M = _Noop()  # type: ignore

class _HTTPServer(HTTPServer):
    allow_reuse_address = True  # important on Windows fast restarts

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/metrics", "/health"):
            self.send_response(404); self.end_headers(); return

        srv: MetricsHTTP = getattr(self.server, "_owner", None)  # type: ignore[attr-defined]
        service = srv.service if srv else "metrics"
        started_at = srv.started_at if srv else time.time()

        if self.path == "/health":
            stat_cb = srv.stat_cb if srv else None
            stat = {}
            if callable(stat_cb):
                try: stat = stat_cb() or {}
                except Exception: stat = {}
            payload = {
                "ok": True,
                "service": service,
                "uptime_s": int(time.time() - started_at),
                "stat": stat,
            }
            body = (json.dumps(payload) + "\n").encode("utf-8")
            ctype = "application/json"
        else:
            text = M.scrape()
            if not isinstance(text, str): text = str(text or "")
            if not text.endswith("\n"): text += "\n"
            body = text.encode("utf-8")
            ctype = 'text/plain; version=0.0.4'

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try: self.wfile.write(body)
        except Exception: pass  # client closed

    def log_message(self, *_args, **_kw):  # silence default
        pass

class MetricsHTTP:
    """
    Tiny /metrics and /health server.

    Args:
        service: label to report in /health (e.g., "trader", "ingestor")
        port:    port to bind (default 7001)
        stat_cb: optional callable returning dict to enrich /health
    """
    def __init__(self, service: str = "metrics", port: int = 7001,
                 stat_cb: Optional[Callable[[], dict]] = None):
        self.service = service
        self.port = int(port)
        self.stat_cb = stat_cb
        self._thread: Optional[threading.Thread] = None
        self._httpd: Optional[_HTTPServer] = None
        self._stop_evt = threading.Event()
        self.started_at = time.time()

    def start(self):
        if self._thread and self._thread.is_alive():
            return self  # idempotent
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, name=f"metrics_http:{self.port}", daemon=False)
        self._thread.start()
        return self

    def _run(self):
        try:
            self._httpd = _HTTPServer(("0.0.0.0", self.port), _Handler)
            setattr(self._httpd, "_owner", self)  # give handler access
            self._httpd.timeout = 1.0
        except PermissionError as e:
            _log("error", "metrics_bind_failed", port=self.port, err=str(e))
            return

        _log("info", "metrics_listening", service=self.service, port=self.port)
        while not self._stop_evt.is_set():
            try:
                self._httpd.handle_request()
            except Exception:
                # keep serving; don't crash on a single bad request
                pass

        try:
            self._httpd.server_close()
        finally:
            self._httpd = None
        _log("info", "metrics_stopped", service=self.service, port=self.port)

    def stop(self):
        self._stop_evt.set()
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                pass
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

def start_metrics_http(
    service_or_port: Union[str, int] = "metrics",
    maybe_port: Optional[int] = None,
    stat_cb: Optional[Callable[[], dict]] = None
):
    """
    Backward/forward-compatible entrypoint.

    Supports:
      - start_metrics_http(SERVICE, PORT, stat_cb=...)
      - start_metrics_http(PORT, stat_cb=...)

    Returns: MetricsHTTP handle with .stop()
    """
    if isinstance(service_or_port, int):
        # old style: (port, stat_cb=...)
        service = "metrics"
        port = service_or_port
    else:
        # new style: (service, port, stat_cb)
        service = service_or_port or "metrics"
        port = maybe_port or 7001
    return MetricsHTTP(service=service, port=port, stat_cb=stat_cb).start()
# trader/broker/alpaca.py