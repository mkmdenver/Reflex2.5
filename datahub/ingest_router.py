# datahub/ingest_router.py
from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional, Callable

from common.bus import CHANNELS, publisher, pack


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# If true, DataHub will synthesize 1-minute bars from trade prints (ticks).
DATAHUB_BUILD_BARS_FROM_TICKS = os.getenv("DATAHUB_BUILD_BARS_FROM_TICKS", "0") in (
    "1", "true", "TRUE", "yes", "YES",
)

# Optional: gate tick publication by tier.
# Default "COLD" means: publish ticks for all tiers (backward-compatible).
DATAHUB_TICK_PUB_MIN_TIER = (os.getenv("DATAHUB_TICK_PUB_MIN_TIER", "COLD") or "COLD").upper().strip()

# Optional: gate quote publication by tier (default HOT if you want strictness, but keep backward compat as COLD)
DATAHUB_QUOTE_PUB_MIN_TIER = (os.getenv("DATAHUB_QUOTE_PUB_MIN_TIER", "COLD") or "COLD").upper().strip()

_TIER_RANK = {"COLD": 0, "WATCH": 1, "WARM": 2, "HOT": 3}


def _tier_ge(tier: str, min_tier: str) -> bool:
    return _TIER_RANK.get((tier or "COLD").upper(), 0) >= _TIER_RANK.get((min_tier or "COLD").upper(), 0)


def _bars_channel() -> str:
    # DataHub is LIVE; publish to bars1m_live if present, else bars1m.
    return CHANNELS.get("bars1m_live", CHANNELS["bars1m"])


def _ticks_channel() -> str:
    return CHANNELS.get("ticks_live", CHANNELS["ticks"])


def _quotes_channel() -> str:
    return CHANNELS.get("quotes_live", CHANNELS["quotes"])


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class Router:
    """
    IngestRouter: receives normalized events from adapter stream and publishes
    to Redis channels.

    worker.py calls:
      - await router.start()
      - router.set_tier_getter(fn)
      - await router.on_tick(sym, msg)
      - await router.on_quote_tob(sym, msg)
      - await router.on_bar_1m(sym, msg)
    """

    def __init__(self) -> None:
        self._pub = None
        self._get_tier: Optional[Callable[[str], str]] = None

        # basic counters (optional/loggable)
        self.stats_ticks_pub = 0
        self.stats_quotes_pub = 0
        self.stats_bars_pub = 0

        # If building bars from ticks, keep rolling per-symbol per-minute accumulator
        self._bar_state: Dict[str, Dict[str, Any]] = {}

    async def start(self) -> None:
        if self._pub is None:
            self._pub = await publisher()

    def set_tier_getter(self, fn: Callable[[str], str]) -> None:
        self._get_tier = fn

    def _tier(self, sym: str) -> str:
        if self._get_tier is None:
            return "COLD"
        try:
            t = (self._get_tier(sym) or "COLD").upper()
            return t if t in _TIER_RANK else "COLD"
        except Exception:
            return "COLD"

    async def _publish(self, channel: str, obj: Dict[str, Any]) -> None:
        if self._pub is None:
            self._pub = await publisher()
        await self._pub.publish(channel, pack(obj))

    # ----------------------------
    # Event handlers
    # ----------------------------

    async def on_tick(self, sym: str, msg: Dict[str, Any]) -> None:
        sym = (sym or "").upper().strip()
        if not sym:
            return

        tier = self._tier(sym)
        if not _tier_ge(tier, DATAHUB_TICK_PUB_MIN_TIER):
            # Strict tick gating: WATCH gets bars only; ticks begin at WARM.
            return

        # Publish tick (normalized by PolygonStream already, but tolerate variants)
        out = dict(msg)
        out.setdefault("type", out.get("ev") or "trade")
        out.setdefault("symbol", sym)
        out["tier"] = tier
        out["hub_ts_ns"] = int(time.time() * 1_000_000_000)

        await self._publish(_ticks_channel(), out)
        self.stats_ticks_pub += 1

        if DATAHUB_BUILD_BARS_FROM_TICKS:
            await self._maybe_build_bar_from_tick(sym, out)

    async def on_quote_tob(self, sym: str, msg: Dict[str, Any]) -> None:
        sym = (sym or "").upper().strip()
        if not sym:
            return

        tier = self._tier(sym)
        if not _tier_ge(tier, DATAHUB_QUOTE_PUB_MIN_TIER):
            return

        out = dict(msg)
        out.setdefault("type", out.get("ev") or "quote")
        out.setdefault("symbol", sym)
        out["tier"] = tier
        out["hub_ts_ns"] = int(time.time() * 1_000_000_000)

        await self._publish(_quotes_channel(), out)
        self.stats_quotes_pub += 1

    async def on_bar_1m(self, sym: str, msg: Dict[str, Any]) -> None:
        """
        Publish canonical 1m bars (Polygon AM).
        WATCH tier should get these without enabling ticks.
        """
        sym = (sym or "").upper().strip()
        if not sym:
            return

        tier = self._tier(sym)

        out = dict(msg)
        out.setdefault("type", out.get("ev") or "bar_1m")
        out.setdefault("symbol", sym)
        out["tier"] = tier
        out["hub_ts_ns"] = int(time.time() * 1_000_000_000)

        # Normalize common bar fields (keep both for compatibility)
        # upstream ws.py provides: o,h,l,c,v,vw,start_ns,end_ns
        # some consumers expect: open/high/low/close/volume or t
        if "open" not in out and "o" in out:
            out["open"] = out["o"]
        if "high" not in out and "h" in out:
            out["high"] = out["h"]
        if "low" not in out and "l" in out:
            out["low"] = out["l"]
        if "close" not in out and "c" in out:
            out["close"] = out["c"]
        if "volume" not in out and "v" in out:
            out["volume"] = out["v"]

        # Provide a single timestamp key that taps/bots can use:
        # use start_ns (bar open) if present
        if "t" not in out:
            if out.get("start_ns"):
                out["t"] = int(out["start_ns"] // 1_000_000)  # ms epoch for human tools
            elif out.get("end_ns"):
                out["t"] = int(out["end_ns"] // 1_000_000)

        await self._publish(_bars_channel(), out)
        self.stats_bars_pub += 1

    # ----------------------------
    # Optional bar synthesis from ticks
    # ----------------------------

    async def _maybe_build_bar_from_tick(self, sym: str, tick: Dict[str, Any]) -> None:
        """
        Very simple 1m bar builder from trade prints. Only used when
        DATAHUB_BUILD_BARS_FROM_TICKS=1.
        """
        # Determine minute bucket from sip_timestamp if present, else now.
        ts_ns = tick.get("sip_timestamp") or tick.get("t") or tick.get("sip_ts_ns")
        if isinstance(ts_ns, (int, float)) and ts_ns > 10_000_000_000:
            # assume ns-ish
            bucket_ms = int((int(ts_ns) // 1_000_000) // 60_000 * 60_000)
        else:
            now_ms = int(time.time() * 1000)
            bucket_ms = (now_ms // 60_000) * 60_000

        px = tick.get("price") or tick.get("p")
        sz = tick.get("size") or tick.get("s") or 0
        try:
            px_f = float(px)
        except Exception:
            return
        try:
            sz_i = int(sz)
        except Exception:
            sz_i = 0

        st = self._bar_state.get(sym)
        if st is None or st.get("t") != bucket_ms:
            # flush previous (if any)
            if st is not None:
                await self._publish(_bars_channel(), st)
                self.stats_bars_pub += 1
            # start new
            st = {
                "type": "bar_1m",
                "symbol": sym,
                "t": bucket_ms,
                "o": px_f,
                "h": px_f,
                "l": px_f,
                "c": px_f,
                "v": sz_i,
                "tier": self._tier(sym),
                "source": "ticks",
                "hub_ts_ns": int(time.time() * 1_000_000_000),
            }
            self._bar_state[sym] = st
            return

        # update existing
        st["c"] = px_f
        st["h"] = max(float(st["h"]), px_f)
        st["l"] = min(float(st["l"]), px_f)
        st["v"] = int(st.get("v", 0)) + sz_i
        st["hub_ts_ns"] = int(time.time() * 1_000_000_000)
