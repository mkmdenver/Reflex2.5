# datahub/adapters/live_adapter.py
#
# LIVE (Polygon) adapter for DataHub.
#
# Responsibilities:
#   - Manage a Polygon websocket connection.
#   - Keep track of current subscriptions (trades / quotes / 1m bars).
#   - Provide a synchronous stream() interface that yields events.
#
# This adapter is deliberately simple: it only knows about symbol-level
# subscriptions. Higher-level tier logic in DataHub decides *what* to
# subscribe to.

from __future__ import annotations

import logging
import os
import queue
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set

from common.polygon_api.ws import PolygonStream, WSConfig  # your existing wrapper

logger = logging.getLogger("datahub.live_adapter")

Event = Dict[str, Any]


@dataclass
class LIVEAdapter:
    """
    LIVE adapter for Polygon streaming.

    Public surface expected by callers:

        adapter = LIVEAdapter(api_key="...", ws_url=None)
        adapter.open()        # or adapter.start()
        adapter.subscribe(trades=[...], quotes=[...], bars_1m=[...])
        for ev in adapter.stream():
            ...

    The DataHub worker also uses:

        adapter.update_subscriptions(trades=[...], quotes=[...], bars_1m=[...])

    Bars:
        - We treat Polygon's 1-minute aggregates as the canonical 1m bars.
        - The underlying PolygonStream is expected to map those to events
          that this adapter will see in _on_event(ev).
        - This class only handles subscription bookkeeping; bar semantics
          live in the adapter / router.
    """

    api_key: str
    ws_url: Optional[str] = None

    # Internal state
    _cfg: Optional[WSConfig] = field(default=None, init=False)
    _stream: Optional[PolygonStream] = field(default=None, init=False)

    _current_trades: Set[str] = field(default_factory=set, init=False)
    _current_quotes: Set[str] = field(default_factory=set, init=False)
    _current_bars_1m: Set[str] = field(default_factory=set, init=False)

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
            self._current_bars_1m.clear()
            # clear queue best-effort
            with self._pending.mutex:
                self._pending.queue.clear()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------
    def subscribe(
        self,
        trades: Iterable[str] = (),
        quotes: Iterable[str] = (),
        bars_1m: Iterable[str] = (),
    ) -> None:
        """
        Subscribe to additional trade / quote / 1m bar symbols.

        NOTE: For bars_1m, the underlying PolygonStream must support
        either:
          - a dedicated method (e.g. subscribe_bars_1m), or
          - a 'bars_1m' keyword on its subscribe() call.
        """
        if self._stream is None:
            logger.warning("LIVEAdapter.subscribe(): stream is not open yet")
            return

        new_trades = {s.upper() for s in trades}
        new_quotes = {s.upper() for s in quotes}
        new_bars_1m = {s.upper() for s in bars_1m}

        t_to_add = sorted(new_trades - self._current_trades)
        q_to_add = sorted(new_quotes - self._current_quotes)
        b_to_add = sorted(new_bars_1m - self._current_bars_1m)

        if not t_to_add and not q_to_add and not b_to_add:
            return

        logger.info(
            "LIVEAdapter.subscribe(): add_trades=%s add_quotes=%s add_bars_1m=%s "
            "current_trades=%s current_quotes=%s current_bars_1m=%s",
            t_to_add,
            q_to_add,
            b_to_add,
            sorted(self._current_trades),
            sorted(self._current_quotes),
            sorted(self._current_bars_1m),
        )

        try:
            # ---- IMPORTANT WIRING POINT ---------------------------------
            # Adapt this block to whatever your PolygonStream exposes for
            # 1-minute bar subscriptions.
            #
            # Example possibilities:
            #   self._stream.subscribe(trades=t_to_add, quotes=q_to_add, bars_1m=b_to_add)
            #   self._stream.subscribe(trades=t_to_add, quotes=q_to_add, aggs_1m=b_to_add)
            #   self._stream.subscribe_trades_quotes_bars1m(t_to_add, q_to_add, b_to_add)
            #
            # For now we try a conservative approach: call subscribe() with
            # trades/quotes, and if you want bars wired you can edit this
            # to match your polygon_api.ws implementation.
            if hasattr(self._stream, "subscribe"):
                # You can extend this signature in your PolygonStream wrapper.
                self._stream.subscribe(trades=t_to_add, quotes=q_to_add)
                # If you later add bars_1m support at the PolygonStream level,
                # wire it here and/or behind a hasattr() guard.
                if b_to_add and hasattr(self._stream, "subscribe_bars_1m"):
                    self._stream.subscribe_bars_1m(b_to_add)  # type: ignore[attr-defined]
                elif b_to_add and DATAHUB_ENABLE_BARS_1M:
                    logger.warning(
                        "LIVEAdapter.subscribe(): bars_1m requested %s but "
                        "PolygonStream has no subscribe_bars_1m; "
                        "wire this in common.polygon_api.ws",
                        b_to_add,
                    )
            else:
                logger.error("LIVEAdapter.subscribe(): underlying stream has no subscribe()")
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"subscribe error: {exc}"
            logger.exception("LIVEAdapter.subscribe(): error: %s", exc)
            return

        self._current_trades.update(t_to_add)
        self._current_quotes.update(q_to_add)
        self._current_bars_1m.update(b_to_add)

    def unsubscribe(
        self,
        trades: Iterable[str] = (),
        quotes: Iterable[str] = (),
        bars_1m: Iterable[str] = (),
    ) -> None:
        """Unsubscribe from the given trade / quote / 1m bar symbols."""
        if self._stream is None:
            logger.warning("LIVEAdapter.unsubscribe(): stream is not open yet")
            return

        rem_trades = {s.upper() for s in trades}
        rem_quotes = {s.upper() for s in quotes}
        rem_bars_1m = {s.upper() for s in bars_1m}

        t_to_remove = sorted(rem_trades & self._current_trades)
        q_to_remove = sorted(rem_quotes & self._current_quotes)
        b_to_remove = sorted(rem_bars_1m & self._current_bars_1m)

        if not t_to_remove and not q_to_remove and not b_to_remove:
            return

        logger.info(
            "LIVEAdapter.unsubscribe(): remove_trades=%s remove_quotes=%s remove_bars_1m=%s "
            "current_trades=%s current_quotes=%s current_bars_1m=%s",
            t_to_remove,
            q_to_remove,
            b_to_remove,
            sorted(self._current_trades),
            sorted(self._current_quotes),
            sorted(self._current_bars_1m),
        )

        try:
            if hasattr(self._stream, "unsubscribe"):
                self._stream.unsubscribe(trades=t_to_remove, quotes=q_to_remove)
                if b_to_remove and hasattr(self._stream, "unsubscribe_bars_1m"):
                    self._stream.unsubscribe_bars_1m(b_to_remove)  # type: ignore[attr-defined]
                elif b_to_remove:
                    logger.warning(
                        "LIVEAdapter.unsubscribe(): bars_1m removal requested %s but "
                        "PolygonStream has no unsubscribe_bars_1m; "
                        "wire this in common.polygon_api.ws",
                        b_to_remove,
                    )
            else:
                logger.error("LIVEAdapter.unsubscribe(): underlying stream has no unsubscribe()")
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"unsubscribe error: {exc}"
            logger.exception("LIVEAdapter.unsubscribe(): error: %s", exc)
            return

        self._current_trades.difference_update(t_to_remove)
        self._current_quotes.difference_update(q_to_remove)
        self._current_bars_1m.difference_update(b_to_remove)

    def update_subscriptions(
        self,
        trades: Iterable[str],
        quotes: Iterable[str],
        bars_1m: Iterable[str] = (),
    ) -> None:
        """
        Set-based subscription update used by the DataHub refresh loop.
        """
        target_trades = {s.upper() for s in trades}
        target_quotes = {s.upper() for s in quotes}
        target_bars_1m = {s.upper() for s in bars_1m}

        t_to_add = sorted(target_trades - self._current_trades)
        t_to_remove = sorted(self._current_trades - target_trades)

        q_to_add = sorted(target_quotes - self._current_quotes)
        q_to_remove = sorted(self._current_quotes - target_quotes)

        b_to_add = sorted(target_bars_1m - self._current_bars_1m)
        b_to_remove = sorted(self._current_bars_1m - target_bars_1m)

        logger.info(
            "LIVEAdapter.update_subscriptions(): "
            "target_trades=%s target_quotes=%s target_bars_1m=%s "
            "add_trades=%s remove_trades=%s "
            "add_quotes=%s remove_quotes=%s "
            "add_bars_1m=%s remove_bars_1m=%s",
            sorted(target_trades),
            sorted(target_quotes),
            sorted(target_bars_1m),
            t_to_add,
            t_to_remove,
            q_to_add,
            q_to_remove,
            b_to_add,
            b_to_remove,
        )

        if t_to_add or q_to_add or b_to_add:
            self.subscribe(trades=t_to_add, quotes=q_to_add, bars_1m=b_to_add)
        if t_to_remove or q_to_remove or b_to_remove:
            self.unsubscribe(
                trades=t_to_remove, quotes=q_to_remove, bars_1m=b_to_remove
            )

    # ------------------------------------------------------------------
    # Event loop / streaming
    # ------------------------------------------------------------------
    def stream(self) -> Iterator[Event]:
        """
        Blocking generator that yields events from PolygonStream.

        Events are whatever PolygonStream's on_event callback receives,
        typically dicts with keys like "ev", "sym", etc., including
        trade / quote / 1m bar events.
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
