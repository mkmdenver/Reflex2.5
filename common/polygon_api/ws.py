# common/polygon_api/ws.py
from __future__ import annotations

import json
import random
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Dict, Any, List, Set, Tuple

try:
    from websocket import WebSocketApp
except Exception as e:  # pragma: no cover
    raise RuntimeError("websocket-client is required (pip install websocket-client). "
                       f"Import error: {e}")

Json = Dict[str, Any]
StrSet = Set[str]


# ----------------------------- Config --------------------------------- #

@dataclass
class WSConfig:
    """
    Backward-compatible WebSocket config for PolygonStream.

    Supports:
      - explicit channel lists via `trades` / `quotes`
      - legacy mixed `subs` list like ["T.AAPL","Q.AAPL","T.*","Q.*"]
      - legacy time/backoff fields with `_s` suffix and older names.
      - legacy passthrough `sslopt` dict merged into internal TLS options.
    """
    url: str = "wss://socket.polygon.io/stocks"
    api_key: str = ""

    # Modern explicit subscription lists (symbols only, e.g., ["AAPL","AMD"] or ["*"])
    trades: Optional[List[str]] = None
    quotes: Optional[List[str]] = None

    # Legacy mixed subscription list: ["T.AAPL","Q.AAPL","T.*","Q.*"]
    subs: Optional[List[str]] = None

    # Keepalive (websocket-client pings), seconds
    ping_interval: int = 10
    ping_timeout: int = 5

    # Legacy alias fields (seconds). If provided, they override the modern names.
    ping_interval_s: Optional[int] = None
    ping_timeout_s: Optional[int] = None

    # Reconnect backoff (seconds)
    max_backoff: float = 8.0
    # Legacy aliases for max backoff
    max_backoff_s: Optional[float] = None
    reconnect_max_sleep_s: Optional[float] = None
    reconnect_max_sleep: Optional[float] = None
    reconnect_backoff_max_s: Optional[float] = None
    reconnect_backoff_max: Optional[float] = None

    # Threading / queues
    workers: int = 4
    inbound_queue_max: int = 50_000
    outbound_queue_max: int = 5_000

    # Small batch subscribe on auth (avoid long on_open stalls)
    resub_batch_size: int = 50
    resub_batch_sleep: float = 0.05

    # TLS options
    verify_cert: bool = True

    # Optional decorative Origin header (some edge networks like explicit origin)
    origin: str = "https://polygon.io"

    # Legacy passthrough for websocket-client SSL options
    sslopt: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        # Map legacy *_s fields if present
        if self.ping_interval_s is not None:
            self.ping_interval = int(self.ping_interval_s)
        if self.ping_timeout_s is not None:
            self.ping_timeout = int(self.ping_timeout_s)

        # Map legacy backoff names
        for candidate in (
            self.max_backoff_s,
            self.reconnect_max_sleep_s,
            self.reconnect_max_sleep,
            self.reconnect_backoff_max_s,
            self.reconnect_backoff_max,
        ):
            if candidate is not None:
                try:
                    self.max_backoff = float(candidate)
                    break
                except Exception:
                    pass


# ---------------------------- Stream ---------------------------------- #

class PolygonStream:
    """
    Production-safe WS driver for Polygon.io.

    Back-compat with older code:
      - __init__(config=WSConfig(...), on_message=..., on_status=..., on_error=...)
      - run_forever() method
    Newer code can also call: PolygonStream(cfg, on_event=...); start()/stop().
    """

    # ---- Back-compat constructor wrapper ----
    def __init__(
        self,
        cfg: Optional[WSConfig] = None,
        on_event: Optional[Callable[[Json], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        **kwargs
    ) -> None:
        """
        Accept either:
          PolygonStream(config=WSConfig(...), on_message=..., on_status=..., on_error=...)
        or:
          PolygonStream(WSConfig(...), on_event=..., on_status=..., on_error=...)
        """
        # Map legacy keyword names from kwargs if used
        legacy_config = kwargs.pop("config", None)
        legacy_on_message = kwargs.pop("on_message", None)
        # tolerate any extra kwargs (ignored)

        cfg = cfg or legacy_config
        if cfg is None or not isinstance(cfg, WSConfig):
            raise TypeError("PolygonStream requires a WSConfig via positional 'cfg' or keyword 'config'.")

        # Choose the callback (prefer explicit on_event; else legacy on_message)
        cb = on_event or legacy_on_message
        if cb is None:
            raise TypeError("PolygonStream requires an event callback via 'on_event' or legacy 'on_message'.")

        self.cfg = cfg
        self.on_event = cb
        self.on_status = on_status or (lambda s: None)
        self.on_error = on_error or (lambda e: None)

        # runtime state
        self._lock = threading.RLock()
        self._stop_evt = threading.Event()
        self._connected_evt = threading.Event()

        # queues: inbound raw frames, outbound control messages (auth/sub)
        from queue import Queue
        self._in_q: "Queue[str]" = Queue(maxsize=cfg.inbound_queue_max)
        self._out_q: "Queue[str]" = Queue(maxsize=cfg.outbound_queue_max)

        self._in_workers: List[threading.Thread] = []
        self._sender_thread: Optional[threading.Thread] = None
        self._runner_thread: Optional[threading.Thread] = None

        # subscription state we maintain and reapply after reconnects
        self._subs: Dict[str, StrSet] = {"T": set(), "Q": set()}      # per channel
        self._want_wildcard: Dict[str, bool] = {"T": False, "Q": False}

        # ws app instance (lives only inside runner thread)
        self._wsapp: Optional[WebSocketApp] = None

        # last activity for watchdog/metrics
        self._last_msg_ts = time.time()

        # ssl options (defaults) + merge in any legacy cfg.sslopt
        self.sslopt: Dict[str, Any] = {
            "cert_reqs": ssl.CERT_REQUIRED if cfg.verify_cert else ssl.CERT_NONE
        }
        if cfg.sslopt:
            try:
                self.sslopt.update(cfg.sslopt)
            except Exception:
                pass

        # seed initial subs (explicit first)
        if cfg.trades:
            self.set_trades(cfg.trades)
        if cfg.quotes:
            self.set_quotes(cfg.quotes)
        # legacy mixed input
        if cfg.subs:
            t_syms, q_syms, t_wc, q_wc = _split_mixed_subs(cfg.subs)
            with self._lock:
                if t_wc:
                    self._want_wildcard["T"] = True
                if q_wc:
                    self._want_wildcard["Q"] = True
                self._subs["T"].update(t_syms)
                self._subs["Q"].update(q_syms)
            # enqueue subscribe messages for both channels
            self._enqueue_sub("T")
            self._enqueue_sub("Q")

    # ------------------------- Public API ------------------------------ #

    def start(self) -> None:
        """Start WS runner + workers + sender."""
        self._stop_evt.clear()

        # spawn WS runner (NON-DAEMON so process won't drop-through)
        if self._runner_thread is None or not self._runner_thread.is_alive():
            self._runner_thread = threading.Thread(
                target=self._run_forever_loop, name="PolygonWS-runner", daemon=False
            )
            self._runner_thread.start()

        # sender (drains _out_q onto the socket)
        if self._sender_thread is None or not self._sender_thread.is_alive():
            self._sender_thread = threading.Thread(
                target=self._sendq_pump, name="PolygonWS-sender", daemon=False
            )
            self._sender_thread.start()

        # inbound workers
        if not self._in_workers:
            for i in range(max(1, self.cfg.workers)):
                t = threading.Thread(
                    target=self._in_pump, name=f"PolygonWS-in-{i}", daemon=False
                )
                t.start()
                self._in_workers.append(t)

        self.on_status("ws_start")

    def stop(self, timeout: float = 3.0) -> None:
        """Signal shutdown and attempt a clean close."""
        self._stop_evt.set()
        try:
            if self._wsapp:
                self._wsapp.close()
        except Exception:
            pass

        # join foreground threads
        if self._runner_thread and self._runner_thread.is_alive():
            self._runner_thread.join(timeout=timeout)
        if self._sender_thread and self._sender_thread.is_alive():
            self._sender_thread.join(timeout=timeout)

        # NEW: also join inbound workers so we don't leave them running
        for t in self._in_workers:
            if t.is_alive():
                t.join(timeout=timeout)

        self.on_status("ws_stopped")

    # ---- Back-compat blocking runner ----
    def run_forever(self) -> None:
        """
        Block the caller thread while the internal WS thread runs.
        Mirrors websocket-client API used by older code.
        """
        self.start()
        try:
            while not self._stop_evt.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    # ------------- Subscription management (thread-safe) --------------- #

    def set_trades(self, symbols: Iterable[str]) -> None:
        self._set_subs("T", symbols)

    def set_quotes(self, symbols: Iterable[str]) -> None:
        self._set_subs("Q", symbols)

    def subscribe(self, trades: Iterable[str] = (), quotes: Iterable[str] = ()) -> None:
        self._merge_subs("T", trades)
        self._merge_subs("Q", quotes)

    def unsubscribe(self, trades: Iterable[str] = (), quotes: Iterable[str] = ()) -> None:
        with self._lock:
            for ch, syms in (("T", trades), ("Q", quotes)):
                for s in syms:
                    s = s.strip().upper()
                    if s == "*" and self._want_wildcard[ch]:
                        self._want_wildcard[ch] = False
                    self._subs[ch].discard(s)
                self._enqueue_unsub(ch)

    # -------------------------- Internals ------------------------------ #

    def _set_subs(self, ch: str, symbols: Iterable[str]) -> None:
        with self._lock:
            want_wc = False
            new: StrSet = set()
            for s in symbols:
                s = s.strip().upper()
                if s == "*":
                    want_wc = True
                else:
                    if s:
                        new.add(s)
            self._want_wildcard[ch] = want_wc
            self._subs[ch] = new
            self._enqueue_sub(ch)

    def _merge_subs(self, ch: str, symbols: Iterable[str]) -> None:
        with self._lock:
            for s in symbols:
                s = s.strip().upper()
                if s == "*":
                    self._want_wildcard[ch] = True
                elif s:
                    self._subs[ch].add(s)
            self._enqueue_sub(ch)

    def _enqueue_sub(self, ch: str) -> None:
        with self._lock:
            if self._want_wildcard[ch]:
                params = f"{ch}.*"
                self._enqueue(json.dumps({"action": "subscribe", "params": params}))
                return
            syms = sorted(self._subs[ch])
        batch: List[str] = []
        for s in syms:
            batch.append(f"{ch}.{s}")
            if len(batch) >= self.cfg.resub_batch_size:
                params = ",".join(batch)
                self._enqueue(json.dumps({"action": "subscribe", "params": params}))
                batch = []
        if batch:
            self._enqueue(json.dumps({"action": "subscribe", "params": ",".join(batch)}))

    def _enqueue_unsub(self, ch: str) -> None:
        self._enqueue_sub(ch)

    def _enqueue(self, s: str) -> None:
        try:
            self._out_q.put_nowait(s)
        except Exception:
            self.on_error("send queue full; dropping control message")

    # ------------------------ Runner / Callbacks ----------------------- #

    def _spawn_ws(self) -> WebSocketApp:
        headers = []
        if self.cfg.origin:
            headers.append(f"Origin: {self.cfg.origin}")

        self._wsapp = WebSocketApp(
            self.cfg.url,
            header=headers,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        return self._wsapp

    def _run_forever_loop(self) -> None:
        backoff = 1.0
        max_backoff = max(1.0, float(self.cfg.max_backoff))
        while not self._stop_evt.is_set():
            try:
                self.on_status("ws_connecting")
                ws = self._spawn_ws()
                ws.run_forever(
                    ping_interval=max(2, int(self.cfg.ping_interval)),
                    ping_timeout=max(2, int(self.cfg.ping_timeout)),
                    sslopt=self.sslopt,
                    origin=self.cfg.origin,
                    skip_utf8_validation=True,
                    http_proxy_host=None,
                    http_proxy_port=None,
                    sockopt=[(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)],
                )
            except Exception as e:
                self.on_error(f"run_forever error: {e!r}")

            self._connected_evt.clear()
            if self._stop_evt.is_set():
                break
            sleep = min(backoff, max_backoff)
            jitter = random.uniform(0.0, sleep * 0.2)
            self.on_status(f"ws_reconnect_sleep:{sleep + jitter:.1f}s")
            time.sleep(sleep + jitter)
            backoff = min(backoff * 2.0, max_backoff)

    # websocket-client callbacks

    def _on_open(self, _ws: WebSocketApp) -> None:
        self._connected_evt.set()
        self._last_msg_ts = time.time()
        self.on_status("ws_open")
        self._enqueue(json.dumps({"action": "auth", "params": self.cfg.api_key}))

    def _on_message(self, _ws: WebSocketApp, message: str) -> None:
        self._last_msg_ts = time.time()
        try:
            self._in_q.put_nowait(message)
        except Exception:
            self.on_error("inbound queue full; dropping message")

    def _on_error(self, _ws: WebSocketApp, error: Exception | str) -> None:
        self.on_status("ws_error")
        self.on_error(str(error))

    def _on_close(self, _ws: WebSocketApp, status_code: Optional[int], msg: Optional[str]) -> None:
        self.on_status(f"ws_close:{status_code}:{msg or ''}")

    # ----------------------- Pumps / Workers --------------------------- #

    def _sendq_pump(self) -> None:
        # wait until connected or stop
        while not self._stop_evt.is_set():
            if self._connected_evt.wait(timeout=0.5):
                break

        while not self._stop_evt.is_set():
            try:
                payload = self._out_q.get(timeout=0.5)
            except Exception:
                continue
            try:
                if self._wsapp:
                    self._wsapp.send(payload)
            except Exception as e:
                self.on_error(f"send error: {e!r}")
                try:
                    self._out_q.put_nowait(payload)
                except Exception:
                    pass
                time.sleep(0.25)
            finally:
                try:
                    self._out_q.task_done()
                except Exception:
                    pass

    def _in_pump(self) -> None:
        while not self._stop_evt.is_set():
            try:
                raw = self._in_q.get(timeout=0.5)
            except Exception:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                try:
                    self._in_q.task_done()
                except Exception:
                    pass
                continue

            events = data if isinstance(data, list) else [data]
            for ev in events:
                et = str(ev.get("ev") or ev.get("event") or "").upper()
                if et == "STATUS" or et == "":
                    status = str(ev.get("status") or "").lower()
                    message = str(ev.get("message") or "").lower()
                    if "auth" in message and "success" in status:
                        self.on_status("ws_authed")
                        self._resubscribe_all()
                    continue

                if et.startswith("T"):
                    norm = _norm_trade(ev)
                    if norm.get("symbol") and norm.get("price"):
                        self.on_event(norm)
                elif et.startswith("Q"):
                    norm = _norm_quote(ev)
                    if norm.get("symbol") and (norm.get("bid") is not None or norm.get("ask") is not None):
                        self.on_event(norm)
                else:
                    self.on_event(ev)
            try:
                self._in_q.task_done()
            except Exception:
                pass

    # ------------------------- Helpers -------------------------------- #

    def _resubscribe_all(self) -> None:
        with self._lock:
            want_wc_T = self._want_wildcard["T"]
            want_wc_Q = self._want_wildcard["Q"]
            list_T = sorted(self._subs["T"])
            list_Q = sorted(self._subs["Q"])

        if want_wc_T:
            self._enqueue(json.dumps({"action": "subscribe", "params": "T.*"}))
        if want_wc_Q:
            self._enqueue(json.dumps({"action": "subscribe", "params": "Q.*"}))

        def chunk_and_send(prefix: str, syms: List[str]) -> None:
            batch: List[str] = []
            for s in syms:
                batch.append(f"{prefix}.{s}")
                if len(batch) >= self.cfg.resub_batch_size:
                    self._enqueue(json.dumps({"action": "subscribe", "params": ",".join(batch)}))
                    batch = []
            if batch:
                self._enqueue(json.dumps({"action": "subscribe", "params": ",".join(batch)}))

        chunk_and_send("T", list_T)
        chunk_and_send("Q", list_Q)


# -------------------- Normalization & utils --------------------------- #

def _to_ns(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        iv = int(v)
        if iv < 10_000_000_000:
            return iv * 1_000_000_000
        return iv
    except Exception:
        return None


def _norm_trade(msg: Json) -> Json:
    return {
        "type": "trade",
        "symbol": msg.get("sym") or msg.get("S") or msg.get("symbol"),
        "price": msg.get("p") or msg.get("price"),
        "size": msg.get("s") or msg.get("size"),
        "sip_timestamp": _to_ns(msg.get("t") or msg.get("sip_timestamp")),
        "exchange": msg.get("x") or msg.get("exchange"),
        "id": msg.get("i") or msg.get("id"),
        "conditions": msg.get("c") or msg.get("conditions") or [],
        "tape": msg.get("z") or msg.get("tape"),
    }


def _norm_quote(msg: Json) -> Json:
    return {
        "type": "quote",
        "symbol": msg.get("sym") or msg.get("S") or msg.get("symbol"),
        "bid": msg.get("bp") if msg.get("bp") is not None else msg.get("bid"),
        "ask": msg.get("ap") if msg.get("ap") is not None else msg.get("ask"),
        "bid_size": msg.get("bs") if msg.get("bs") is not None else msg.get("bid_size"),
        "ask_size": msg.get("as") if msg.get("as") is not None else msg.get("ask_size"),
        "sip_timestamp": _to_ns(msg.get("t") or msg.get("sip_timestamp")),
        "exchange": msg.get("x") or msg.get("exchange"),
    }


def _split_mixed_subs(entries: Iterable[str]) -> Tuple[StrSet, StrSet, bool, bool]:
    t_syms: StrSet = set()
    q_syms: StrSet = set()
    t_wc = q_wc = False
    for raw in entries or []:
        s = (raw or "").strip().upper()
        if not s:
            continue
        if s == "T.*":
            t_wc = True
            continue
        if s == "Q.*":
            q_wc = True
            continue
        if s.startswith("T.") and len(s) > 2:
            t_syms.add(s[2:])
        elif s.startswith("Q.") and len(s) > 2:
            q_syms.add(s[2:])
    return t_syms, q_syms, t_wc, q_wc


__all__ = ["WSConfig", "PolygonStream"]
# EOF
