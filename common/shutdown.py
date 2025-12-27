# common/shutdown.py
import atexit, signal, threading, sys, traceback

class StopHandle:
    """Wraps a callable that gracefully closes something."""
    def __init__(self, name, closer):
        self.name = name
        self.closer = closer

class ShutdownManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._stops: list[StopHandle] = []
        self._stopped = False
        atexit.register(self._run_all, reason="atexit")

        # Windows: SIGINT (Ctrl+C) works. SIGTERM is delivered by taskkill /PID N (no /F).
        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig:
                signal.signal(sig, self._signal_handler)

    def register(self, name: str, closer):
        with self._lock:
            self._stops.append(StopHandle(name, closer))

    def _signal_handler(self, signum, frame):
        print(f"[Shutdown] signal {signum} received — stopping services…", file=sys.stderr)
        self._run_all(reason=f"signal {signum}")
        # Let main unwind
        sys.exit(0)

    def _run_all(self, reason="unknown"):
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            for sh in reversed(self._stops):
                try:
                    print(f"[Shutdown] -> {sh.name}")
                    sh.closer()
                except Exception:
                    traceback.print_exc()

shutdown_mgr = ShutdownManager()
