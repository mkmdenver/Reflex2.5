# datahub/tier_manager.py
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Dict, Iterable, List, Optional, Set, Tuple

class Tier(IntEnum):
    COLD = 0
    WATCH = 1
    WARM = 2
    HOT = 3
    TRADE = 4

    @classmethod
    def parse(cls, v: object) -> "Tier":
        if isinstance(v, Tier):
            return v
        if v is None:
            return Tier.COLD
        s = str(v).strip().upper()
        # accept legacy aliases
        if s in ("TRADE", "INTRADE", "TRADING"):
            return Tier.TRADE
        if s in ("HOT",):
            return Tier.HOT
        if s in ("WARM",):
            return Tier.WARM
        if s in ("WATCH", "WL", "WATCHLIST"):
            return Tier.WATCH
        if s in ("COLD",):
            return Tier.COLD
        # allow numeric
        try:
            n = int(s)
            return Tier(n)
        except Exception:
            return Tier.COLD

DEFAULT_TTL_SEC: Dict[Tier, float] = {
    Tier.COLD: 24 * 3600,
    Tier.WATCH: 60 * 60,      # 1h
    Tier.WARM: 15 * 60,       # 15m
    Tier.HOT: 4 * 60,         # 4m
    Tier.TRADE: 12 * 3600,    # essentially "sticky"; should be refreshed by Trader in real tie-in
}

# Needs are symbolic: "bars", "snap", "ticks", "quotes"
VALID_NEEDS = {"bars", "snap", "ticks", "quotes"}

@dataclass
class Claim:
    symbol: str
    tier: Tier
    needs: Set[str] = field(default_factory=set)
    source: str = "unknown"
    ts: float = 0.0
    expires_at: float = 0.0
    score: float = 0.0

@dataclass
class Effective:
    symbol: str
    tier: Tier
    needs: Set[str]

class TierManager:
    """
    Central, thread-safe tier + stream-needs manager.

    Model:
      - callers submit *claims* with TTL (tier + optional needs)
      - DataHub computes an effective tier (max) and effective needs (union)
      - a GC pass expires old claims and recomputes state

    This keeps bots dumb: they raise evidence, DataHub handles decay/cleanup.
    """
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._claims: Dict[str, List[Claim]] = {}  # symbol -> claims list

    def add_claim(
        self,
        symbol: str,
        tier: Tier,
        *,
        needs: Iterable[str] = (),
        source: str = "unknown",
        ts: Optional[float] = None,
        ttl_sec: Optional[float] = None,
        score: float = 0.0,
    ) -> None:
        sym = symbol.strip().upper()
        if not sym:
            return
        now = time.time()
        ts_val = float(ts) if ts is not None else now
        ttl = float(ttl_sec) if ttl_sec is not None else float(DEFAULT_TTL_SEC.get(tier, 600.0))
        # Normalize needs
        needs_set: Set[str] = set()
        for n in needs or ():
            try:
                ns = str(n).strip().lower()
            except Exception:
                continue
            if ns in VALID_NEEDS:
                needs_set.add(ns)

        c = Claim(
            symbol=sym,
            tier=tier,
            needs=needs_set,
            source=(source or "unknown"),
            ts=ts_val,
            expires_at=ts_val + max(1.0, ttl),
            score=float(score or 0.0),
        )
        with self._lock:
            self._claims.setdefault(sym, []).append(c)

    def gc(self, now: Optional[float] = None) -> None:
        t = float(now) if now is not None else time.time()
        with self._lock:
            dead = []
            for sym, lst in self._claims.items():
                nlst = [c for c in lst if c.expires_at > t]
                if nlst:
                    self._claims[sym] = nlst
                else:
                    dead.append(sym)
            for sym in dead:
                self._claims.pop(sym, None)

    def effective(self) -> List[Effective]:
        out: List[Effective] = []
        with self._lock:
            for sym, lst in self._claims.items():
                if not lst:
                    continue
                # max tier wins; union needs across non-expired claims
                tier = max((c.tier for c in lst), default=Tier.COLD)
                needs: Set[str] = set()
                for c in lst:
                    needs |= c.needs
                out.append(Effective(symbol=sym, tier=tier, needs=needs))
        return out

    
    def get_effective(self, symbol: str) -> Tier:
        sym = symbol.strip().upper()
        with self._lock:
            lst = self._claims.get(sym) or []
            if not lst:
                return Tier.COLD
            return max((c.tier for c in lst), default=Tier.COLD)

def counts(self) -> Dict[str, int]:
        # effective counts by tier (ignoring needs)
        eff = self.effective()
        c = {"COLD": 0, "WATCH": 0, "WARM": 0, "HOT": 0, "TRADE": 0}
        for e in eff:
            c[e.tier.name] = c.get(e.tier.name, 0) + 1
        return c
