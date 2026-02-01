# common/polygon_api/ws.py
from __future__ import annotations

import json
import random
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Dict, Any, List, Set

try:
    from websocket import WebSocketApp
except Exception as e:  # pragma: no cover
    raise RuntimeError(
        "websocket-client is required (pip install websocket-client). "
        f"Import error: {e}"
    )

Json = Dict[str, Any]
StrSet = Set[str]


def _sleep_s(seconds: float) -> None:
    time.sleep(max(0.0, seconds))


def _to_ns(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        x = int(v)
    except Exception:
        return None
    if x <= 0:
        return None
    # ns?
    if x > 10_000_000_000_000_000:
        return x
    # us?
    if x > 10_000_000_000_000:
        return x * 1000
    # ms?
    if x > 10_000_000_000:
        return x * 1_000_000
    # seconds
    if x > 10_000_000:
        return x * 1_000_000_000
    # assume ms (small)
    return x * 1_000_000


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
        "tape": msg.get("z") or msg.get("tape"),
    }


def _norm_bar_1m(msg: Json) -> Json:
    # Polygon minute aggregate: ev=AM
    return {
        "type": "bar_1m",
        "symbol": msg.get("sym") or msg.get("S") or msg.get("symbol"),
        "o": msg.get("o") or msg.get("open"),
        "h": msg.get("h") or msg.get("high"),
        "l": msg.get("l") or msg.get("low"),
        "c": msg.get("c") or msg.get("close"),
        "v": msg.get("v") or msg.get("volume"),
        "vw": msg.get("vw"),
        "av": msg.get("av"),
        "op": msg.get("op"),
        "start_ns": _to_ns(msg.get("s") or msg.get("start")),
        "end_ns": _to_ns(msg.get("e") or msg.get("end")),
    }


@dataclass
class WSConfig:
    url: str = "wss://socket.polygon.io/stocks"
    api_key: str = ""

    trades: Optional[List[str]] = None
    quotes: Optional[List[str]] = None
    bars_1m: Optional[List[str]] = None  # AM.*

    subs: Optional[List[str]] = None

    ping_interval: int = 10
    ping_timeout: int = 5
    ping_interval_s: Optional[int] = None
    ping_timeout_s: Optional[int] = None

    max_backoff: float = 8.0
    max_backoff_s: Optional[float] = None
    reconnect_max_sleep_s: Optional[float] = None
    reconnect_max_sleep: Optional[float] = None

    resub_batch_size: int = 150
    sslopt: Optional[Dict[str, Any]] = None


class PolygonStream:
    def __init__(
        self,
        cfg: WSConfig,
        on_event: Callable[[Json], None],
        on_status: Callable[[str], None] = lambda _s: None,
        on_error: Callable[[str], None] = lambda _e: None,
    ) -> None:
        self.cfg = cfg
        self.on_event = on_event
        self.on_status = on_status
        self.on_error = on_error

        self._lock = threading.Lock()
        self._subs: Dict[str, StrSet] = {"T": set(), "Q": set(), "AM": set()}
        self._want_wildcard: Dict[str, bool] = {"T": False, "Q": False, "AM": False}

        self._ws: Optional[WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None

        self._in_q: "queue.Queue[str]" = __import__("queue").Queue(maxsize=50_000)
        self._out_q: "queue.Queue[str]" = __import__("queue").Queue(maxsize=50_000)

        self._stop = threading.Event()

        # pump threads (must exist, or nothing works)
        self._in_thread: Optional[threading.Thread] = None
        self._out_thread: Optional[threading.Thread] = None

        self._prime_subs_from_cfg()

    # -------------------------- Public API ---------------------------- #

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        # START THE PUMPS (missing today)
        if not hasattr(self, "_in_thread"):
            self._in_thread = None
        if not hasattr(self, "_out_thread"):
            self._out_thread = None

        if self._in_thread is None or not self._in_thread.is_alive():
            self._in_thread = threading.Thread(target=self._in_pump, name="polygon_ws_in", daemon=True)
            self._in_thread.start()

        if self._out_thread is None or not self._out_thread.is_alive():
            self._out_thread = threading.Thread(target=self._out_pump, name="polygon_ws_out", daemon=True)
            self._out_thread.start()

        # Existing ws runner
        self._thread = threading.Thread(target=self._run, name="polygon_ws", daemon=True)
        self._thread.start()


    def set_trades(self, symbols: Iterable[str]) -> None:
        self._set_subs("T", symbols)

    def set_quotes(self, symbols: Iterable[str]) -> None:
        self._set_subs("Q", symbols)

    def set_bars_1m(self, symbols: Iterable[str]) -> None:
        self._set_subs("AM", symbols)

    def subscribe_bars_1m(self, symbols: Iterable[str]) -> None:
        self._merge_subs("AM", symbols)

    def unsubscribe_bars_1m(self, symbols: Iterable[str]) -> None:
        with self._lock:
            for s in symbols:
                s = s.strip().upper()
                if s == "*" and self._want_wildcard["AM"]:
                    self._want_wildcard["AM"] = False
                self._subs["AM"].discard(s)
            self._enqueue("__SUBCHANGE__")

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
            self._enqueue("__SUBCHANGE__")

    # -------------------------- Internals ------------------------------ #

    def _prime_subs_from_cfg(self) -> None:
        if self.cfg.trades is not None:
            self.set_trades(self.cfg.trades)
        if self.cfg.quotes is not None:
            self.set_quotes(self.cfg.quotes)
        if self.cfg.bars_1m is not None:
            self.set_bars_1m(self.cfg.bars_1m)

        if self.cfg.subs:
            t_syms: List[str] = []
            q_syms: List[str] = []
            am_syms: List[str] = []
            for s in self.cfg.subs:
                s = (s or "").strip()
                if not s or "." not in s:
                    continue
                ch, sym = s.split(".", 1)
                ch, sym = ch.upper(), sym.upper()
                if ch == "T":
                    t_syms.append(sym)
                elif ch == "Q":
                    q_syms.append(sym)
                elif ch == "AM":
                    am_syms.append(sym)
            if t_syms:
                self.set_trades(t_syms)
            if q_syms:
                self.set_quotes(q_syms)
            if am_syms:
                self.set_bars_1m(am_syms)

    def _set_subs(self, ch: str, symbols: Iterable[str]) -> None:
        with self._lock:
            want_wc = False
            new: StrSet = set()
            for s in symbols:
                s = s.strip().upper()
                if s == "*":
                    want_wc = True
                elif s:
                    new.add(s)
            self._subs[ch] = new
            self._want_wildcard[ch] = want_wc
        self._enqueue("__SUBCHANGE__")

    def _merge_subs(self, ch: str, symbols: Iterable[str]) -> None:
        changed = False
        with self._lock:
            for s in symbols:
                s = s.strip().upper()
                if not s:
                    continue
                if s == "*":
                    if not self._want_wildcard[ch]:
                        self._want_wildcard[ch] = True
                        changed = True
                else:
                    if s not in self._subs[ch]:
                        self._subs[ch].add(s)
                        changed = True
        if changed:
            self._enqueue("__SUBCHANGE__")

    def _enqueue(self, payload: str) -> None:
        try:
            self._out_q.put_nowait(payload)
        except Exception:
            self.on_error("outbound queue full; dropping payload")

    def _run(self) -> None:
        backoff = 0.25
        while not self._stop.is_set():
            try:
                self._connect_and_loop()
                backoff = 0.25
            except Exception as e:
                self.on_error(f"ws loop error: {e!r}")
            _sleep_s(backoff + random.random() * 0.25)
            backoff = min(
                getattr(self.cfg, "max_backoff_s", None)
                or getattr(self.cfg, "reconnect_max_sleep_s", None)
                or getattr(self.cfg, "reconnect_max_sleep", None)
                or self.cfg.max_backoff,
                backoff * 2,
            )

    def _connect_and_loop(self) -> None:
        sslopt = {"cert_reqs": ssl.CERT_REQUIRED}
        if self.cfg.sslopt:
            sslopt.update(self.cfg.sslopt)

        ping_interval = self.cfg.ping_interval_s if self.cfg.ping_interval_s is not None else self.cfg.ping_interval
        ping_timeout = self.cfg.ping_timeout_s if self.cfg.ping_timeout_s is not None else self.cfg.ping_timeout

        self._ws = WebSocketApp(
            self.cfg.url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )

        self._ws.run_forever(
            sslopt=sslopt,
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
            ping_payload="ping",
            skip_utf8_validation=True,
            sockopt=((socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),),
        )

    def _on_open(self, _ws: WebSocketApp) -> None:
        self.on_status("ws_open")
        # auth must be sent via out pump
        self._enqueue(json.dumps({"action": "auth", "params": self.cfg.api_key}))

    def _on_message(self, _ws: WebSocketApp, message: str) -> None:
        try:
            self._in_q.put_nowait(message)
        except Exception:
            self.on_error("inbound queue full; dropping message")

    def _on_error(self, _ws: WebSocketApp, error: Exception | str) -> None:
        self.on_status("ws_error")
        self.on_error(str(error))

    def _on_close(self, _ws: WebSocketApp, status_code: Optional[int], msg: Optional[str]) -> None:
        self.on_status(f"ws_close:{status_code}:{msg or ''}")

    def _resubscribe_all(self) -> None:
        with self._lock:
            want_wc_T = self._want_wildcard["T"]
            want_wc_Q = self._want_wildcard["Q"]
            want_wc_AM = self._want_wildcard["AM"]
            list_T = sorted(self._subs["T"])
            list_Q = sorted(self._subs["Q"])
            list_AM = sorted(self._subs["AM"])

        if want_wc_T:
            self._enqueue(json.dumps({"action": "subscribe", "params": "T.*"}))
        if want_wc_Q:
            self._enqueue(json.dumps({"action": "subscribe", "params": "Q.*"}))
        if want_wc_AM:
            self._enqueue(json.dumps({"action": "subscribe", "params": "AM.*"}))

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
        chunk_and_send("AM", list_AM)

    def _in_pump(self) -> None:
        while not self._stop.is_set():
            try:
                raw = self._in_q.get(timeout=0.5)
            except Exception:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue

            events = data if isinstance(data, list) else [data]
            for ev in events:
                et = str(ev.get("ev") or ev.get("event") or "").upper()

                if et == "STATUS" or et == "":
                    status = str(ev.get("status") or "").lower()
                    message = str(ev.get("message") or "").lower()

                    # ALWAYS surface status so you can see "not authorized" / "subscribed" etc.
                    try:
                        self.on_status(f"ws_status:{status}:{message}")
                    except Exception:
                        pass

                    # Accept the common variants Polygon uses
                    authed = (
                        ("auth" in message and "success" in status) or
                        ("authenticated" in message) or
                        ("auth_success" in status) or
                        ("successfully authenticated" in message)
                    )
                    if authed:
                        self.on_status("ws_authed")
                        self._resubscribe_all()
                    continue


                if et.startswith("T"):
                    norm = _norm_trade(ev)
                    if norm.get("symbol") and norm.get("price") is not None:
                        self.on_event(norm)
                elif et.startswith("Q"):
                    norm = _norm_quote(ev)
                    if norm.get("symbol") and (norm.get("bid") is not None or norm.get("ask") is not None):
                        self.on_event(norm)
                elif et == "AM":
                    norm = _norm_bar_1m(ev)
                    if norm.get("symbol") and (norm.get("o") is not None) and (norm.get("c") is not None):
                        self.on_event(norm)

    def _out_pump(self) -> None:
        while not self._stop.is_set():
            try:
                payload = self._out_q.get(timeout=0.5)
            except Exception:
                continue

            try:
                if payload == "__SUBCHANGE__":
                    # Re-send full subscription set.
                    self._resubscribe_all()
                else:

                    if self._ws:
                        if '"action": "subscribe"' in payload or '"action":"subscribe"' in payload:
                            print("[polygon.ws] SEND", payload)
                        self._ws.send(payload)

                        if self._ws:
                            self._ws.send(payload)
            except Exception:
                pass
