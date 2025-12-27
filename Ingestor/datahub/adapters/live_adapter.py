# datahub/adapters/live_adapter.py
#
# LIVE (Polygon) adapter for DataHub.
#
# Responsibilities:
#   - Manage a Polygon websocket connection.
#   - Keep track of current subscriptions (trades/quotes).
#   - Provide a synchronous stream() interface that yields events.
#
# This adapter is deliberately simple: it only knows about symbol-level
# trade/quote subscriptions. The higher-level tier logic in DataHub
# decides *what* to subscribe to.

from __future__ import annotations

import logging
import queue
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set

from common.polygon_api.ws import PolygonStream, WSConfig

logger = logging.getLogger("datahub.live_adapter")

Event = Dict[str, Any]


@dataclass
class LIVEAdapter:
    """
    LIVE adapter for Polygon streaming.

    Public surface expected by callers:

        adapter = LIVEAdapter(api_key="...", ws_url=None)
        adapter.open()        # or adapter.start()
        adapter.subscribe(trades=[...], quotes=[...])
        for ev in adapter.stream():
            ...

    The DataHub worker also uses:

        adapter.update_subscriptions(trades=[...], quotes=[...])
    """

    api_key: str
    ws_url: Optional[str] = None

    # Internal state
    _cfg: Optional[WSConfig] = field(default=None, init=False)
    _stream: Optional[PolygonStream] = field(default=None, init=False)

    _current_trades: Set[str] = field(default_factory=set, init=False)
    _current_quotes: Set[str] = field(default_factory=set, init=False)

    _pending: "queue.Queue[Event]" = field(
        default_factory=lambda: queue.Queue(maxsize=10_000), init=False
    )

    _last_error: Optional[str] = field(default=None, init=False)
    _status: str = field(default="init", init=False)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Alias for open(), for compatibility with MarketAdapter."""
        self.open()

    def open(self) -> None:
        """
        Build a WSConfig and start the PolygonStream.

        We start with no explicit symbol subscriptions; the DataHub
        refresh loop will call update_subscriptions() shortly after boot.
        """
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

        # Start the underlying WS threads
        self._stream.start()
        self._status = "running"

    def close(self) -> None:
        """Stop the PolygonStream and clear state."""
        if self._stream is None:
            return
        logger.info("LIVEAdapter.close()")
        try:
            self._stream.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("LIVEAdapter.close(): stop() error: %s", exc)
        finally:
            self._stream = None
            self._current_trades.clear()
            self._current_quotes.clear()
            # clear queue best-effort
            with self._pending.mutex:
                self._pending.queue.clear()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------
    def subscribe(self, trades: Iterable[str] = (), quotes: Iterable[str] = ()) -> None:
        """Subscribe to additional trade/quote symbols."""
        if self._stream is None:
            logger.warning("LIVEAdapter.subscribe(): stream is not open yet")
            return

        new_trades = {s.upper() for s in trades}
        new_quotes = {s.upper() for s in quotes}

        t_to_add = sorted(new_trades - self._current_trades)
        q_to_add = sorted(new_quotes - self._current_quotes)

        if not t_to_add and not q_to_add:
            return

        logger.info(
            "LIVEAdapter.subscribe(): add_trades=%s add_quotes=%s current_trades=%s current_quotes=%s",
            t_to_add,
            q_to_add,
            sorted(self._current_trades),
            sorted(self._current_quotes),
        )

        try:
            self._stream.subscribe(trades=t_to_add, quotes=q_to_add)
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"subscribe error: {exc}"
            logger.exception("LIVEAdapter.subscribe(): error: %s", exc)
            return

        self._current_trades.update(t_to_add)
        self._current_quotes.update(q_to_add)

    def unsubscribe(self, trades: Iterable[str] = (), quotes: Iterable[str] = ()) -> None:
        """Unsubscribe from the given trade/quote symbols."""
        if self._stream is None:
            logger.warning("LIVEAdapter.unsubscribe(): stream is not open yet")
            return

        rem_trades = {s.upper() for s in trades}
        rem_quotes = {s.upper() for s in quotes}

        t_to_remove = sorted(rem_trades & self._current_trades)
        q_to_remove = sorted(rem_quotes & self._current_quotes)

        if not t_to_remove and not q_to_remove:
            return

        logger.info(
            "LIVEAdapter.unsubscribe(): remove_trades=%s remove_quotes=%s current_trades=%s current_quotes=%s",
            t_to_remove,
            q_to_remove,
            sorted(self._current_trades),
            sorted(self._current_quotes),
        )

        try:
            self._stream.unsubscribe(trades=t_to_remove, quotes=q_to_remove)
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"unsubscribe error: {exc}"
            logger.exception("LIVEAdapter.unsubscribe(): error: %s", exc)
            return

        self._current_trades.difference_update(t_to_remove)
        self._current_quotes.difference_update(q_to_remove)

    def update_subscriptions(self, trades: Iterable[str], quotes: Iterable[str]) -> None:
        """
        Set-based subscription update used by the DataHub refresh loop.
        """
        target_trades = {s.upper() for s in trades}
        target_quotes = {s.upper() for s in quotes}

        t_to_add = sorted(target_trades - self._current_trades)
        t_to_remove = sorted(self._current_trades - target_trades)

        q_to_add = sorted(target_quotes - self._current_quotes)
        q_to_remove = sorted(self._current_quotes - target_quotes)

        logger.info(
            "LIVEAdapter.update_subscriptions(): target_trades=%s target_quotes=%s "
            "add_trades=%s remove_trades=%s add_quotes=%s remove_quotes=%s",
            sorted(target_trades),
            sorted(target_quotes),
            t_to_add,
            t_to_remove,
            q_to_add,
            q_to_remove,
        )

        if t_to_add or q_to_add:
            self.subscribe(trades=t_to_add, quotes=q_to_add)
        if t_to_remove or q_to_remove:
            self.unsubscribe(trades=t_to_remove, quotes=q_to_remove)

    # ------------------------------------------------------------------
    # Event loop / streaming
    # ------------------------------------------------------------------
    def stream(self) -> Iterator[Event]:
        """
        Blocking generator that yields events from PolygonStream.

        Events are whatever PolygonStream's on_event callback receives,
        typically dicts with keys like "ev", "sym", "p", etc.
        """
        logger.info("LIVEAdapter.stream(): starting event loop")
        while True:
            ev = self._pending.get()
            yield ev

    # ------------------------------------------------------------------
    # Callbacks from PolygonStream
    # ------------------------------------------------------------------
    def _on_event(self, ev: Event) -> None:
        """Called by PolygonStream when a normalized event arrives."""
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
