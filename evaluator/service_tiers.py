# evaluator/service_tiers.py
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Literal

from common.bus import publish_sync, CHANNELS

# Alias for backward-compat with older code that referenced `publish`
def publish(channel: str, payload: Dict) -> None:
    """
    Thin wrapper so existing code can call `publish(...)` in sync contexts.

    Under the hood this calls common.bus.publish_sync, which:
      - Uses the shared REDIS_URL / CHANNELS from common.bus
      - Safely handles the presence/absence of an existing event loop
    """
    publish_sync(channel, payload)


TierName = Literal["COLD", "WATCH", "WARM", "HOT"]


@dataclass
class ModelTierState:
    """
    Per-model view of symbol interest tiers.

    This tracks, for a given model (strategy, filter, etc.), what the
    *highest* tier requested so far is for each symbol. It prevents us
    from spamming DataHub with redundant "raise to WARM" or "raise to HOT"
    events when we're already at or above that tier.
    """

    tiers: Dict[str, TierName] = field(default_factory=dict)

    # Rank ordering of tiers for comparison.
    _rank: Dict[TierName, int] = field(
        default_factory=lambda: {
            "COLD": 0,
            "WATCH": 1,
            "WARM": 2,
            "HOT": 3,
        }
    )

    def get(self, symbol: str) -> TierName:
        """
        Get the current tier this model believes `symbol` is at.

        Defaults to "COLD" if we've never seen the symbol before.
        """
        return self.tiers.get(symbol.upper(), "COLD")

    def maybe_raise(self, symbol: str, tier: TierName) -> bool:
        """
        Update local view to `tier` if it is strictly higher than current.

        Returns True if we actually changed something (i.e. caller should
        publish a raise event), False if this would be a no-op.
        """
        symbol = symbol.upper()
        cur = self.get(symbol)
        if self._rank[tier] > self._rank[cur]:
            self.tiers[symbol] = tier
            return True
        return False


def request_service_level(symbol: str, tier: TierName, source: str) -> None:
    """
    Ask DataHub to provide at-least `tier` service for `symbol`.

    This sends a fire-and-forget message to CHANNELS["raise"] that the
    TierRequestListener in DataHub will pick up.

    Typically used by:
      - coarse filters (raise to WATCH/WARM)
      - pattern models / combo loops (raise to HOT)
    """
    ev = {
        "symbol": symbol.upper(),
        "tier": tier,
        "source": source,
        "ts": time.time_ns(),
    }
    publish(CHANNELS["raise"], ev)
