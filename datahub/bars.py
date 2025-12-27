from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone, date
from typing import Dict, List, Optional, Tuple, Any


@dataclass
class MinuteBar:
    """
    Simple 1-minute OHLCV bar.

    ts_min: epoch second at the *start* of the minute bucket (UTC).
    """
    ts_min: int
    open: float
    high: float
    low: float
    close: float
    volume: int


class MinuteBarManager:
    """
    In-memory per-symbol intraday bar store.

    Responsibilities:
      - Given a stream of trades (symbol, price, size, ts), maintain:
          * today's 1-minute bars per symbol
          * a computed "today" daily bar per symbol
      - Expose helpers to query:
          * last two completed minute closes
          * recent minute bars
          * today's daily bar snapshot

    This does *not* talk to DB, HTTP, or Redis. It is purely in-process.
    """

    def __init__(self, max_minutes: int = 600) -> None:
        # max_minutes is a soft cap on how many completed bars we keep per symbol.
        self._max_minutes = max_minutes
        self._lock = threading.RLock()
        self._day: Optional[date] = None
        self._bars: Dict[str, List[MinuteBar]] = {}
        self._current: Dict[str, MinuteBar] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_trade(self, msg: Dict[str, Any]) -> None:
        """
        Ingest a trade message and update the minute / daily state.

        Expected fields in msg:
          - "symbol" (or "sym")
          - "price"  (or "p")
          - "size"   (or "qty")
          - some timestamp field:
                * "ts" (epoch seconds)
                * "timestamp"
                * "t"
                * "sip_timestamp"
          If no usable timestamp is found, time.time() will be used
          as a fallback.

        NOTE: Field names are intentionally tolerant because different
        feeds use different conventions. Adjust _extract_ts_sec() if your
        feed uses a different shape.
        """
        sym = (msg.get("symbol") or msg.get("sym") or "").upper()
        if not sym:
            return

        price_raw = msg.get("price")
        if price_raw is None:
            # common alternate field
            price_raw = msg.get("p")
        size_raw = msg.get("size")
        if size_raw is None:
            size_raw = msg.get("qty")

        try:
            price = float(price_raw)
        except Exception:
            return
        try:
            size = int(size_raw or 0)
        except Exception:
            size = 0

        ts_sec = self._extract_ts_sec(msg)
        if ts_sec is None:
            # Fallback: treat as "now"
            ts_sec = datetime.now(timezone.utc).timestamp()

        self._update_for_trade(sym, price, size, ts_sec)

    def last_two_minute_closes(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        """
        Return (prev_close, curr_close) for the last two COMPLETED minute bars.

        Does not include the currently-building bar.
        """
        sym = symbol.upper()
        with self._lock:
            bars = self._bars.get(sym, [])
            if len(bars) < 2:
                return None, None
            prev = bars[-2].close
            curr = bars[-1].close
            return prev, curr

    def minute_bars(self, symbol: str, limit: int = 100) -> List[MinuteBar]:
        """
        Return up to `limit` most recent COMPLETED minute bars for a symbol.
        """
        sym = symbol.upper()
        with self._lock:
            bars = self._bars.get(sym, [])
            if not bars:
                return []
            return list(bars[-limit:])

    def daily_bar(self, symbol: str) -> Optional[MinuteBar]:
        """
        Return a synthetic "daily" bar for *today* built from today's minute bars.

        This uses:
          open  = first minute open
          high  = max of minute highs
          low   = min of minute lows
          close = last minute close
          vol   = sum of minute volumes

        If there are no minute bars yet, returns None.
        """
        sym = symbol.upper()
        with self._lock:
            bars = self._bars.get(sym, [])
            if not bars:
                return None

            first = bars[0]
            last = bars[-1]
            high = max(b.high for b in bars)
            low = min(b.low for b in bars)
            vol = sum(b.volume for b in bars)
            # ts_min for daily is arbitrary; use first bar's ts_min.
            return MinuteBar(
                ts_min=first.ts_min,
                open=first.open,
                high=high,
                low=low,
                close=last.close,
                volume=vol,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_ts_sec(self, msg: Dict[str, Any]) -> Optional[float]:
        """
        Try to pull an epoch seconds timestamp out of a trade message.

        Handles ns / ms / s heuristically. If nothing plausible is found,
        returns None.
        """
        candidates = [
            msg.get("ts"),
            msg.get("timestamp"),
            msg.get("t"),
            msg.get("sip_timestamp"),
        ]
        raw = next((c for c in candidates if c is not None), None)
        if raw is None:
            return None
        try:
            val = float(raw)
        except Exception:
            return None

        # Heuristic: ns / ms / s
        #   - ns ~ 1e18+
        #   - ms ~ 1e12+
        #   - s  ~ 1e9+
        if val > 1e16:
            # nanoseconds
            return val / 1_000_000_000.0
        if val > 1e12:
            # microseconds or milliseconds; treat as microseconds-ish
            return val / 1_000_000.0
        if val > 1e10:
            # milliseconds
            return val / 1_000.0
        return val

    def _update_for_trade(self, symbol: str, price: float, size: int, ts_sec: float) -> None:
        dt = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
        today = dt.date()
        minute_bucket = int(ts_sec // 60) * 60  # epoch seconds at minute start

        with self._lock:
            # If we've rolled into a new day, reset all bar state.
            if self._day is None or today != self._day:
                self._day = today
                self._bars.clear()
                self._current.clear()

            sym = symbol.upper()
            cur = self._current.get(sym)

            if cur is None or cur.ts_min != minute_bucket:
                # Close out the previous bar, if any.
                if cur is not None:
                    self._append_completed_bar(sym, cur)

                # Start a new bar for this minute.
                new_bar = MinuteBar(
                    ts_min=minute_bucket,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=size,
                )
                self._current[sym] = new_bar
            else:
                # Update existing bar.
                cur.high = max(cur.high, price)
                cur.low = min(cur.low, price)
                cur.close = price
                cur.volume += size

    def _append_completed_bar(self, sym: str, bar: MinuteBar) -> None:
        arr = self._bars.setdefault(sym, [])
        arr.append(bar)
        # Soft cap to avoid unbounded lists if something goes wrong.
        if len(arr) > self._max_minutes:
            # keep the most recent max_minutes
            del arr[:-self._max_minutes]
