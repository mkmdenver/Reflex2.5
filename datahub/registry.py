# datahub/registry.py
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

from .snapshots import Snapshots
from .buffers import DoubleRingBuffer
from .models import SymbolMeta, SymbolState, SymbolFlags


@dataclass
class MinuteBar:
    """
    Canonical 1-minute bar as delivered by the adapter (Polygon or replay).

    We treat the upstream adapter as the authority for OHLC/V and do not
    recompute OHLC from ticks inside DataHub.
    """

    t_open: int       # epoch seconds for bar open
    t_close: int      # epoch seconds for bar close
    open: float
    high: float
    low: float
    close: float
    volume: float


class Registry:
    """
    Central in-memory state for the DataHub.

    Responsibilities:
      - Last trade / quote snapshots via `Snapshots`
      - Ring buffers of recent trades / quotes per symbol
      - Symbol metadata (state, flags, last price / bid / ask)
      - Intraday 1-minute bars as delivered by the adapter
    """

    def __init__(self, max_bars_1m: int = 64) -> None:
        self.snaps = Snapshots()
        self.trade_bufs: Dict[str, DoubleRingBuffer] = {}
        self.quote_bufs: Dict[str, DoubleRingBuffer] = {}
        self.meta: Dict[str, SymbolMeta] = {}

        # For each symbol we keep a deque of the last N 1-minute bars.
        self.bars_1m: Dict[str, Deque[MinuteBar]] = {}
        self._max_bars_1m = max_bars_1m

        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Symbol metadata / tiers
    # ------------------------------------------------------------------

    def hydrate_symbols(
        self,
        symbols: Iterable[str],
        initial_states: Dict[str, SymbolState],
        flags: Dict[str, SymbolFlags],
    ) -> None:
        """
        Populate initial symbol metadata from DB or config.

        Typically called once at hub boot with symbol_metadata fetched
        from Postgres by an outer component.
        """
        with self._lock:
            for s in symbols:
                sU = s.upper()
                self.meta[sU] = SymbolMeta(
                    symbol=sU,
                    state=initial_states.get(sU, SymbolState.COLD),
                    flags=flags.get(sU, SymbolFlags()),
                )

    def set_flags(self, symbol: str, flags: SymbolFlags) -> None:
        with self._lock:
            m = self.meta.get(symbol.upper())
            if m:
                m.flags = flags

    def set_state(self, symbol: str, state: SymbolState) -> None:
        with self._lock:
            m = self.meta.get(symbol.upper())
            if m:
                m.state = state

    # ------------------------------------------------------------------
    # Ingestion from Router / adapter
    # ------------------------------------------------------------------

    def push_trade(self, msg: Dict[str, Any]) -> None:
        """
        Ingest a trade message from the adapter.

        Expected minimal shape:

            {
              "symbol": "SPY",
              "price":  10.23,
              "size":   100,
              ...
            }
        """
        sym = (msg.get("symbol") or "").upper()
        if not sym:
            return

        # Update snapshots
        self.snaps.on_trade(msg)

        with self._lock:
            buf = self.trade_bufs.setdefault(sym, DoubleRingBuffer())
            buf.push(msg)
            meta = self.meta.setdefault(sym, SymbolMeta(symbol=sym))
            price = msg.get("price")
            if isinstance(price, (int, float)):
                meta.last_price = float(price)

    def push_quote(self, msg: Dict[str, Any]) -> None:
        """
        Ingest a top-of-book quote message from the adapter.

        Expected minimal shape:

            {
              "symbol": "SPY",
              "bid":    10.2,
              "ask":    10.3,
              ...
            }
        """
        sym = (msg.get("symbol") or "").upper()
        if not sym:
            return

        self.snaps.on_quote(msg)

        with self._lock:
            buf = self.quote_bufs.setdefault(sym, DoubleRingBuffer())
            buf.push(msg)
            meta = self.meta.setdefault(sym, SymbolMeta(symbol=sym))
            bid = msg.get("bid")
            ask = msg.get("ask")
            if isinstance(bid, (int, float)):
                meta.last_bid = float(bid)
            if isinstance(ask, (int, float)):
                meta.last_ask = float(ask)

    def push_bar_1m(self, msg: Dict[str, Any]) -> None:
        """
        Ingest a 1-minute bar from the adapter.

        The adapter (live or replay) is responsible for mapping upstream
        bar / agg messages into this canonical shape:

            {
              "symbol":   "SPY",
              "t_open":   1733257200,   # epoch seconds
              "t_close":  1733257259,   # epoch seconds
              "open":     10.10,
              "high":     10.50,
              "low":      10.05,
              "close":    10.40,
              "volume":   123456,
              ...
            }

        We keep the last N such bars per symbol.
        """
        sym = (msg.get("symbol") or "").upper()
        if not sym:
            return

        try:
            bar = MinuteBar(
                t_open=int(msg["t_open"]),
                t_close=int(msg["t_close"]),
                open=float(msg["open"]),
                high=float(msg["high"]),
                low=float(msg["low"]),
                close=float(msg["close"]),
                volume=float(msg.get("volume", 0.0)),
            )
        except Exception:
            # Malformed payload; drop it. The adapter should already log details.
            return

        with self._lock:
            dq = self.bars_1m.get(sym)
            if dq is None:
                dq = deque(maxlen=self._max_bars_1m)
                self.bars_1m[sym] = dq
            dq.append(bar)

            # Optionally update last_price from bar close if we don't have a trade.
            meta = self.meta.setdefault(sym, SymbolMeta(symbol=sym))
            meta.last_price = bar.close

    # ------------------------------------------------------------------
    # Read-side helpers for eval / API
    # ------------------------------------------------------------------

    def get_meta(self, symbol: str) -> Optional[SymbolMeta]:
        with self._lock:
            return self.meta.get(symbol.upper())

    def all_meta(self) -> Dict[str, Dict[str, Any]]:
        """Return a JSON-friendly view of all symbol metadata."""
        with self._lock:
            out: Dict[str, Dict[str, Any]] = {}
            for s, m in self.meta.items():
                out[s] = {
                    "symbol": m.symbol,
                    "state": m.state.name,
                    "flags": {
                        "no_trade": m.flags.no_trade,
                        "halted": m.flags.halted,
                        "restricted": m.flags.restricted,
                        **m.flags.custom,
                    },
                    "last_price": m.last_price,
                    "last_bid": m.last_bid,
                    "last_ask": m.last_ask,
                }
            return out

    def get_last_n_bars_1m(self, symbol: str, n: int) -> List[MinuteBar]:
        """
        Return up to the last *n* 1-minute bars for `symbol`.

        Most eval use-cases will ask for a small N (e.g. 10).
        """
        sym = symbol.upper()
        with self._lock:
            dq = self.bars_1m.get(sym)
            if not dq:
                return []
            if n >= len(dq):
                return list(dq)
            # Newest last, oldest first
            return list(dq)[-n:]

    def get_last_two_closes_1m(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        """
        Return (prev_close, last_close) for 1-minute bars.

        If we have fewer than 2 bars, one or both values may be None.
        """
        bars = self.get_last_n_bars_1m(symbol, 2)
        if not bars:
            return None, None
        if len(bars) == 1:
            return None, bars[0].close
        return bars[-2].close, bars[-1].close

    # ------------------------------------------------------------------
    # Stats for /internal/health or metrics
    # ------------------------------------------------------------------

    def stat(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "symbols_total": len(self.meta),
                "symbols_trades": len(self.trade_bufs),
                "symbols_quotes": len(self.quote_bufs),
                "symbols_bars_1m": len(self.bars_1m),
            }
