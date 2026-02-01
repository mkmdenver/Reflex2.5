from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
import statistics
import time
from typing import Deque, Optional


def _now_s() -> float:
    # monotonic clock avoids wall-clock jumps
    return time.monotonic()


@dataclass
class TslfeConfig:
    # Noise floor: require a new extreme to exceed last extreme by at least eps_price.
    # If you have min_tick, set eps_price=min_tick or 2*min_tick.
    eps_price: float = 0.01

    # How many TBE samples to keep for median normalization
    tbe_window: int = 25

    # Guard: if we have too few samples, norm is None/1.0-ish
    min_samples_for_norm: int = 8


@dataclass
class ChaseStats:
    chase_count: int = 0
    aggress_cents: float = 0.0
    last_replace_ts_s: Optional[float] = None

    def note_replace(self, aggress_cents: float) -> None:
        self.chase_count += 1
        self.aggress_cents = aggress_cents
        self.last_replace_ts_s = _now_s()

    def last_replace_age_ms(self) -> Optional[int]:
        if self.last_replace_ts_s is None:
            return None
        return int((_now_s() - self.last_replace_ts_s) * 1000)


@dataclass
class TradeMetrics:
    # --- Core identity-ish ---
    side_kind: str  # "long" or "short"

    # --- TSLFE state ---
    cfg: TslfeConfig = field(default_factory=TslfeConfig)
    last_extreme_price: Optional[float] = None
    last_extreme_ts_s: Optional[float] = None
    tbe_s_window: Deque[float] = field(default_factory=deque)

    # --- Derived metrics ---
    tslfe_raw_s: float = 0.0
    tslfe_norm: Optional[float] = None
    phase: str = "STEADY"

    # --- A few other “pipes-ready” metrics ---
    chase: ChaseStats = field(default_factory=ChaseStats)
    temperature: float = 0.0  # 0..1 (v1 heuristic)

    def _is_favorable_extreme(self, price: float) -> bool:
        if self.last_extreme_price is None:
            return True
        eps = self.cfg.eps_price
        if self.side_kind == "long":
            return price > (self.last_extreme_price + eps)
        else:
            return price < (self.last_extreme_price - eps)

    def on_tick(self, price: float, ts_s: Optional[float] = None) -> None:
        """
        Call on every tick/quote update used for trade management.
        ts_s is optional monotonic seconds; if None, uses now.
        """
        now = ts_s if ts_s is not None else _now_s()

        # Initialize
        if self.last_extreme_price is None:
            self.last_extreme_price = price
            self.last_extreme_ts_s = now
            self.tslfe_raw_s = 0.0
            self._recompute_norm_and_phase(now)
            return

        # Update favorable extreme?
        if self._is_favorable_extreme(price):
            # record TBE (time between favorable extremes)
            if self.last_extreme_ts_s is not None:
                tbe = max(0.0, now - self.last_extreme_ts_s)
                self.tbe_s_window.append(tbe)
                while len(self.tbe_s_window) > self.cfg.tbe_window:
                    self.tbe_s_window.popleft()

            self.last_extreme_price = price
            self.last_extreme_ts_s = now
            self.tslfe_raw_s = 0.0
        else:
            self.tslfe_raw_s = max(0.0, now - (self.last_extreme_ts_s or now))

        self._recompute_norm_and_phase(now)

    def _recompute_norm_and_phase(self, now_s: float) -> None:
        # Norm
        if len(self.tbe_s_window) >= self.cfg.min_samples_for_norm:
            med = statistics.median(self.tbe_s_window)
            if med > 1e-9:
                self.tslfe_norm = self.tslfe_raw_s / med
            else:
                self.tslfe_norm = None
        else:
            self.tslfe_norm = None

        # Phase: keep it simple; tune later from logs
        n = self.tslfe_norm
        if n is None:
            self.phase = "STEADY"
        elif n < 0.8:
            self.phase = "ACCEL"
        elif n < 1.6:
            self.phase = "STEADY"
        elif n < 2.4:
            self.phase = "DECEL"
        else:
            self.phase = "STALL"

    def note_exit_chase_replace(self, aggress_cents: float) -> None:
        self.chase.note_replace(aggress_cents)

        # Temperature bump from churn (bounded)
        self.temperature = min(1.0, self.temperature + 0.05)

    def cool_temperature(self, dt_s: float) -> None:
        # slow decay; call from your trade loop each second or so
        self.temperature = max(0.0, self.temperature - (0.02 * dt_s))

    def snapshot(self) -> dict:
        return {
            "tslfe_raw_s": round(self.tslfe_raw_s, 4),
            "tslfe_norm": None if self.tslfe_norm is None else round(self.tslfe_norm, 4),
            "phase": self.phase,
            "tbe_med_s": None if len(self.tbe_s_window) == 0 else round(statistics.median(self.tbe_s_window), 4),
            "tbe_n": len(self.tbe_s_window),
            "chase_count": self.chase.chase_count,
            "aggress_cents": round(self.chase.aggress_cents, 2),
            "last_replace_age_ms": self.chase.last_replace_age_ms(),
            "temperature": round(self.temperature, 4),
        }
