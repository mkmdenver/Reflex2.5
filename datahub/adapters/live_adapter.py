# datahub/adapters/live_adapter.py
from __future__ import annotations

import logging
import queue
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, Optional, Set

from common.polygon_api.ws import PolygonStream, WSConfig

logger = logging.getLogger("datahub.live_adapter")
Event = Dict[str, Any]


@dataclass
class LIVEAdapter:
    api_key: str
    ws_url: Optional[str] = None

    _cfg: Optional[WSConfig] = field(default=None, init=False)
    _stream: Optional[PolygonStream] = field(default=None, init=False)

    _current_trades: Set[str] = field(default_factory=set, init=False)
    _current_quotes: Set[str] = field(default_factory=set, init=False)
    _current_bars_1m: Set[str] = field(default_factory=set, init=False)

    _pending: "queue.Queue[Event]" = field(default_factory=lambda: queue.Queue(maxsize=10_000), init=False)

    _last_error: Optional[str] = field(default=None, init=False)
    _status: str = field(default="init", init=False)

    # ----------------------------
    # Lifecycle
    # ----------------------------

    def start(self) -> None:
        self.open()

    def open(self) -> None:
        if self._stream is not None:
            logger.warning("LIVEAdapter.open(): stream already open; ignoring")
            return

        cfg = WSConfig(api_key=self.api_key)
        if self.ws_url:
            cfg.url = self.ws_url
        self._cfg = cfg

        logger.info(
            "LIVEAdapter.open(): url=%s api_key_hint=%s",
            getattr(cfg, "url", None),
            (self.api_key[:4] + "…") if self.api_key else "NONE",
        )

        self._stream = PolygonStream(
            cfg,
            on_event=self._on_event,
            on_status=self._on_status,
            on_error=self._on_error,
        )
        self._stream.start()
        self._status = "running"

    def close(self) -> None:
        if self._stream is None:
            return
        logger.info("LIVEAdapter.close()")
        try:
            self._stream.stop()
        except Exception as exc:
            logger.warning("LIVEAdapter.close(): stop() error: %s", exc)
        finally:
            self._stream = None
            self._current_trades.clear()
            self._current_quotes.clear()
            self._current_bars_1m.clear()
            with self._pending.mutex:
                self._pending.queue.clear()  # type: ignore[attr-defined]

    # ----------------------------
    # Subscription control
    # ----------------------------

    def subscribe(self, trades: Iterable[str] = (), quotes: Iterable[str] = (), bars_1m: Iterable[str] = ()) -> None:
        if self._stream is None:
            logger.warning("LIVEAdapter.subscribe(): stream is not open yet")
            return

        new_trades = {s.strip().upper() for s in trades if s and str(s).strip()}
        new_quotes = {s.strip().upper() for s in quotes if s and str(s).strip()}
        new_bars = {s.strip().upper() for s in bars_1m if s and str(s).strip()}

        t_to_add = sorted(new_trades - self._current_trades)
        q_to_add = sorted(new_quotes - self._current_quotes)
        b_to_add = sorted(new_bars - self._current_bars_1m)

        if not t_to_add and not q_to_add and not b_to_add:
            return

        logger.info(
            "LIVEAdapter.subscribe(): add_trades=%s add_quotes=%s add_bars_1m=%s",
            t_to_add, q_to_add, b_to_add,
        )

        try:
            if t_to_add or q_to_add:
                self._stream.subscribe(trades=t_to_add, quotes=q_to_add)
            if b_to_add:
                self._stream.subscribe_bars_1m(b_to_add)
        except Exception as exc:
            self._last_error = f"subscribe error: {exc}"
            logger.exception("LIVEAdapter.subscribe(): error: %s", exc)
            return

        self._current_trades.update(t_to_add)
        self._current_quotes.update(q_to_add)
        self._current_bars_1m.update(b_to_add)

    def unsubscribe(self, trades: Iterable[str] = (), quotes: Iterable[str] = (), bars_1m: Iterable[str] = ()) -> None:
        if self._stream is None:
            logger.warning("LIVEAdapter.unsubscribe(): stream is not open yet")
            return

        rem_trades = {s.strip().upper() for s in trades if s and str(s).strip()}
        rem_quotes = {s.strip().upper() for s in quotes if s and str(s).strip()}
        rem_bars = {s.strip().upper() for s in bars_1m if s and str(s).strip()}

        t_to_remove = sorted(rem_trades & self._current_trades)
        q_to_remove = sorted(rem_quotes & self._current_quotes)
        b_to_remove = sorted(rem_bars & self._current_bars_1m)

        if not t_to_remove and not q_to_remove and not b_to_remove:
            return

        logger.info(
            "LIVEAdapter.unsubscribe(): remove_trades=%s remove_quotes=%s remove_bars_1m=%s",
            t_to_remove, q_to_remove, b_to_remove,
        )

        try:
            if t_to_remove or q_to_remove:
                self._stream.unsubscribe(trades=t_to_remove, quotes=q_to_remove)
            if b_to_remove:
                self._stream.unsubscribe_bars_1m(b_to_remove)
        except Exception as exc:
            self._last_error = f"unsubscribe error: {exc}"
            logger.exception("LIVEAdapter.unsubscribe(): error: %s", exc)
            return

        self._current_trades.difference_update(t_to_remove)
        self._current_quotes.difference_update(q_to_remove)
        self._current_bars_1m.difference_update(b_to_remove)

    def update_subscriptions(self, trades: Iterable[str], quotes: Iterable[str], bars_1m: Iterable[str] = ()) -> None:
        target_trades = {s.strip().upper() for s in trades if s and str(s).strip()}
        target_quotes = {s.strip().upper() for s in quotes if s and str(s).strip()}
        target_bars = {s.strip().upper() for s in bars_1m if s and str(s).strip()}

        t_to_add = sorted(target_trades - self._current_trades)
        t_to_remove = sorted(self._current_trades - target_trades)

        q_to_add = sorted(target_quotes - self._current_quotes)
        q_to_remove = sorted(self._current_quotes - target_quotes)

        b_to_add = sorted(target_bars - self._current_bars_1m)
        b_to_remove = sorted(self._current_bars_1m - target_bars)

        # *** CRITICAL FIX ***
        # If there is no delta at all, do NOTHING.
        if not (t_to_add or t_to_remove or q_to_add or q_to_remove or b_to_add or b_to_remove):
            return

        logger.info(
            "LIVEAdapter.update_subscriptions(): "
            "add T=%s Q=%s AM=%s | remove T=%s Q=%s AM=%s",
            t_to_add, q_to_add, b_to_add, t_to_remove, q_to_remove, b_to_remove
        )

        if t_to_add or q_to_add or b_to_add:
            self.subscribe(trades=t_to_add, quotes=q_to_add, bars_1m=b_to_add)
        if t_to_remove or q_to_remove or b_to_remove:
            self.unsubscribe(trades=t_to_remove, quotes=q_to_remove, bars_1m=b_to_remove)

    # ----------------------------
    # Event stream
    # ----------------------------

    def stream(self) -> Iterator[Event]:
        logger.info("LIVEAdapter.stream(): starting event loop")
        while True:
            yield self._pending.get()

    def _on_event(self, ev: Event) -> None:
        try:
            self._pending.put_nowait(ev)
        except queue.Full:
            logger.warning("LIVEAdapter._on_event(): queue full; dropping event")

    def _on_status(self, status: str) -> None:
        self._status = status
        logger.info("LIVEAdapter._on_status(): %s", status)

    def _on_error(self, err: Any) -> None:
        self._last_error = str(err)
        logger.exception("LIVEAdapter._on_error(): %s", err)
