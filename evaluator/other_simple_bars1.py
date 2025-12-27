from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Any

from common.logging import info, debug
from evaluator.service_tiers import request_service_level
from evaluator.state import EVAL_STATE

COMPONENT = "model.simple_bars1"


@dataclass
class SimpleBars1Model:
    """
    Extremely simple 4-stage model used as a template:

        Filter1  -> promote from COLD -> WATCH  (minute-level tracking)
        Filter2  -> promote from WATCH -> WARM  (tick-level tracking)
        Pattern  -> look at recent minute bars
        Trigger  -> emit an intent when pattern condition is met

    This concrete instance is the "SPY Momo / SimpleBars1" toy model:

      • Symbol is fixed (SPY) by the outer loop.
      • Pattern is "minute close up by >= offset vs previous minute close".
      • Trigger emits a LONG intent when the pattern fires.

    The goal is not to be a good strategy, but to exercise the template:
      Filter1 → Filter2 → Pattern → Trigger → Intent
    """

    symbol: str
    offset: float                      # price delta threshold, e.g. 0.05 dollars
    instance: str
    mode: str
    source: str = "spy_momo"

    tier: str = field(default="COLD", init=False)

    # --- internal minute-tracking state ---------------------------------
    _current_bucket: Optional[int] = field(default=None, init=False)
    _current_bucket_close: Optional[float] = field(default=None, init=False)
    _last_closed_close: Optional[float] = field(default=None, init=False)
    _current_closed_close: Optional[float] = field(default=None, init=False)

    def _log_info(self, msg: str, **extra: Any) -> None:
        info(
            COMPONENT,
            msg,
            extra={
                "symbol": self.symbol,
                "instance": self.instance,
                "mode": self.mode,
                "tier": self.tier,
                "source": self.source,
                **extra,
            },
        )

    # ------------------------------------------------------------------
    # Filter1 / Filter2: model-tier logic driving DataHub tiers
    # ------------------------------------------------------------------

    def run_filter1(self) -> None:
        """
        Filter1 – runs once on startup.

        For this template we just say:
          "If this model is attached to a symbol at all, WATCH it."
        """
        if self.tier != "COLD":
            return

        self._log_info(
            "filter1.run",
            concept="float < 20M",  # just a placeholder concept for now
        )

        # Promote DataHub tier to WATCH (minute-level tracking)
        request_service_level(symbol=self.symbol, tier="WATCH", source=self.source)
        self.tier = "WATCH"

        self._log_info("filter1.promoted", new_tier="WATCH")

    def run_filter2(self) -> None:
        """
        Filter2 – can run repeatedly; here we invoke it once at startup.

        For SPY Momo template:
          "If symbol == SPY, promote to WARM."
        """
        if self.tier not in ("WATCH", "WARM"):
            # Should only ever be called after Filter1.
            return

        passed = True  # placeholder: symbol == SPY etc.
        self._log_info(
            "filter2.run",
            passed=passed,
            rule="symbol == SPY",
        )
        if not passed:
            return

        # Promote DataHub tier to WARM (tick-level tracking)
        if self.tier != "WARM":
            request_service_level(symbol=self.symbol, tier="WARM", source=self.source)
            self.tier = "WARM"
            self._log_info("filter2.promoted", new_tier="WARM")

    # ------------------------------------------------------------------
    # Pattern / Trigger on minute close-to-close deltas
    # ------------------------------------------------------------------

    def _update_observation(
        self,
        *,
        status: str,
        diff: Optional[float] = None,
        direction: Optional[str] = None,
        fired: bool = False,
    ) -> None:
        """
        Push a compact snapshot into the global Evaluator state so the
        Cockpit panel can visualize model behaviour without digging
        through logs.
        """
        obs = {
            "model": "SimpleBars1",
            "symbol": self.symbol,
            "tier": self.tier,
            "status": status,          # idle | armed | near | fired | error
            "direction": direction,    # up | down | flat | None
            "diff": diff,
            "threshold": self.offset,
            "ratio": abs(diff) / self.offset if (diff is not None and self.offset > 0) else None,
            "fired": fired,
            "stage": "close_to_close",
            "updated_ts": time.time(),
        }
        EVAL_STATE.update_pattern_observation("SimpleBars1", self.symbol, obs)

    def _on_new_closed_minute(self) -> None:
        """
        Invoked whenever a minute bucket finishes and we have at least
        two closed-minute closes.
        """
        if self._last_closed_close is None or self._current_closed_close is None:
            # Not enough history yet for a comparison.
            self._update_observation(status="idle")
            return

        prev_close = self._last_closed_close
        this_close = self._current_closed_close
        diff = this_close - prev_close

        direction: str
        if diff > 0:
            direction = "up"
        elif diff < 0:
            direction = "down"
        else:
            direction = "flat"

        threshold = self.offset
        fired = diff >= threshold
        near = (not fired) and diff >= 0.7 * threshold and threshold > 0

        if fired:
            status = "fired"
        elif near:
            status = "near"
        else:
            status = "armed"

        # *** THIS is your per-minute marker in the evaluator log ***
        self._log_info(
            "pattern.minute_closed",
            prev_close=prev_close,
            this_close=this_close,
            diff=diff,
            threshold=threshold,
            direction=direction,
            status=status,
        )

        if fired:
            # Trigger a LONG intent (no actual order here; just the signal).
            self._log_info(
                "trigger.long",
                reason="minute_close_up",
                prev_close=prev_close,
                this_close=this_close,
                diff=diff,
                threshold=threshold,
            )

        self._update_observation(
            status=status,
            diff=diff,
            direction=direction,
            fired=fired,
        )

    def on_tick(self, tick: Dict[str, Any]) -> None:
        """
        Tick handler – VERY light-weight.

        The only job here is to maintain a rolling view of minute
        closes and call _on_new_closed_minute() when we roll from one
        minute to the next.

        We intentionally avoid any heavy work so that models can scale
        across many symbols.
        """
        # Polygon trade schema usually has:
        #   sym: symbol
        #   p:   price
        #   t:   timestamp (ns)
        # DataHub adds:
        #   t_recv_ns: local receive timestamp (ns)
        sym = tick.get("sym") or tick.get("symbol")
        if sym and sym != self.symbol:
            return

        price = tick.get("p") or tick.get("price") or tick.get("last_price")
        if price is None:
            return

        t_ns = tick.get("t") or tick.get("t_recv_ns")
        if not isinstance(t_ns, int):
            # Fallback: treat as "no minute context", but still
            # update a synthetic bucket so we exercise the pattern
            # machinery in tests.
            t_ns = int(time.time() * 1_000_000_000)

        bucket = t_ns // 60_000_000_000  # 60 seconds in ns

        if self._current_bucket is None:
            # First tick we've ever seen.
            self._current_bucket = bucket
            self._current_bucket_close = float(price)
            self._update_observation(status="idle")
            return

        if bucket == self._current_bucket:
            # Same minute; just update the "close".
            self._current_bucket_close = float(price)
            return

        if bucket < self._current_bucket:
            # Time went backwards; that *should* not happen in live
            # streams, but we won't panic. Just log once and ignore.
            self._log_info(
                "pattern.bucket_time_rollback",
                bucket=bucket,
                current_bucket=self._current_bucket,
            )
            self._update_observation(status="error")
            return

        # We have moved to a new minute. The previous bucket is now a
        # closed minute with a final close price.
        if self._current_bucket_close is not None:
            self._last_closed_close = self._current_closed_close
            self._current_closed_close = self._current_bucket_close

        # Start tracking the new bucket.
        self._current_bucket = bucket
        self._current_bucket_close = float(price)

        # Evaluate the pattern using the *two most recent closed minutes*.
        self._on_new_closed_minute()

    # --- public orchestration API ----------------------------------------

    def run_startup_filters(self) -> None:
        """
        Entry-point used by the loop process:

            1) run Filter1 once
            2) immediately run Filter2 once
        """
        self.run_filter1()
        self.run_filter2()
