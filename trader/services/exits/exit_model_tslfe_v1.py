from __future__ import annotations

from dataclasses import dataclass
import time


@dataclass
class ExitDecision:
    action: str  # "HOLD" | "TRIM" | "FLATTEN"
    delta_qty: int = 0           # for TRIM only (negative)
    reason: str = ""
    meta: dict | None = None


@dataclass
class ExitTslfeV1Config:
    trim_th: float = 1.8          # TSLFE_norm threshold to start trimming
    flatten_th: float = 2.6       # TSLFE_norm threshold to flatten
    trim_pct: float = 0.2         # trim 20% of current_qty per event
    min_trim_shares: int = 1

    # prevent spam trims: minimum time between trim actions
    min_trim_interval_s: float = 2.0


class ExitModelTslfeV1:
    def __init__(self, cfg: ExitTslfeV1Config | None = None) -> None:
        self.cfg = cfg or ExitTslfeV1Config()
        self._last_trim_ts = 0.0

    def decide(self, *, current_qty: int, metrics_snapshot: dict) -> ExitDecision:
        n = metrics_snapshot.get("tslfe_norm", None)
        phase = metrics_snapshot.get("phase", "STEADY")

        # If norm is not ready yet, do nothing (hard stop still protects you)
        if n is None:
            return ExitDecision(action="HOLD", reason="tslfe_not_ready", meta={"phase": phase})

        # Flatten on stall
        if n >= self.cfg.flatten_th or phase == "STALL":
            return ExitDecision(
                action="FLATTEN",
                reason="tslfe_stall",
                meta={"tslfe_norm": n, "phase": phase},
            )

        # Trim on decel
        if n >= self.cfg.trim_th or phase == "DECEL":
            now = time.monotonic()
            if (now - self._last_trim_ts) < self.cfg.min_trim_interval_s:
                return ExitDecision(action="HOLD", reason="trim_rate_limited", meta={"tslfe_norm": n, "phase": phase})

            trim = int(round(current_qty * self.cfg.trim_pct))
            trim = max(self.cfg.min_trim_shares, trim)
            trim = min(trim, max(0, current_qty))  # defensive
            if trim <= 0:
                return ExitDecision(action="HOLD", reason="no_qty_to_trim", meta={"tslfe_norm": n, "phase": phase})

            self._last_trim_ts = now
            return ExitDecision(
                action="TRIM",
                delta_qty=-trim,
                reason="tslfe_decel_trim",
                meta={"tslfe_norm": n, "phase": phase, "trim_shares": trim},
            )

        return ExitDecision(action="HOLD", reason="tslfe_ok", meta={"tslfe_norm": n, "phase": phase})
