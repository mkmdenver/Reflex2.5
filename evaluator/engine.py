
from __future__ import annotations
import json, time, threading
from dataclasses import dataclass
from typing import Optional, Dict, Callable

from .bus import Bus
from .metrics_runtime import set_running, set_pnl, set_drawdown, inc_decision, trades_seen, control_updates
from .strategies import base as Sbase
from .strategies import bullflag  # register

@dataclass
class Controls:
    throttle: int = 100
    torque: float = 1.0
    gear: int = 1
    mix: str = "bullflag"  # strategy key
    paused: bool = False

class EvaluatorEngine:
    def __init__(self, bus_url: str, watch: Optional[set[str]] = None, default_strategy: str = "bullflag"):
        self.bus = Bus(bus_url, decode_responses=False)
        self.watch = {s.upper() for s in (watch or set())}
        self.ctrl = Controls(mix=default_strategy)
        self.strategy = self._make_strategy(default_strategy)
        self._stop = threading.Event()

    def _make_strategy(self, name: str):
        # re-hydrate strategy with current throttle/torque/gear
        return Sbase.create(name, throttle=self.ctrl.throttle, torque=self.ctrl.torque, gear=self.ctrl.gear)

    # --- Controls public API ---
    def get_controls(self) -> dict:
        return dict(throttle=self.ctrl.throttle, torque=self.ctrl.torque, gear=self.ctrl.gear, mix=self.ctrl.mix, paused=self.ctrl.paused)

    def set_controls(self, patch: dict) -> dict:
        changed = False
        if "throttle" in patch:
            self.ctrl.throttle = max(0, int(patch["throttle"])); changed = True
        if "torque" in patch:
            self.ctrl.torque = max(0.1, min(float(patch["torque"]), 10.0)); changed = True
        if "gear" in patch:
            self.ctrl.gear = max(1, min(int(patch["gear"]), 5)); changed = True
        if "mix" in patch and str(patch["mix"]) != self.ctrl.mix:
            self.ctrl.mix = str(patch["mix"]); changed = True
        if "paused" in patch:
            self.ctrl.paused = bool(patch["paused"]); changed = True

        if changed:
            # reconfigure strategy if mix/params changed
            self.strategy = self._make_strategy(self.ctrl.mix)
            # broadcast
            try:
                self.bus.publish("evaluator.controls", self.get_controls())
                control_updates.inc()
            except Exception:
                pass
        set_running(not self.ctrl.paused)
        return self.get_controls()

    # --- Processing ---
    def _on_message(self, channel: str, data: str):
        # controls updates also flow via bus
        if channel == "evaluator.controls":
            try:
                patch = json.loads(data)
                self.set_controls(patch)
            except Exception:
                return
            return

        # trades
        if channel.startswith("md.trades."):
            try:
                msg = json.loads(data)
            except Exception:
                return
            sym = str(msg.get("symbol") or "").upper()
            if not sym: return
            if self.watch and sym not in self.watch: return
            price = msg.get("price")
            if price is None: return
            ts_ns = int(msg.get("ts") or msg.get("sip_timestamp") or 0) or time.time_ns()
            trades_seen.inc()

            if self.ctrl.paused: return
            decision = self.strategy.on_trade(sym, float(price), ts_ns)
            if decision:
                self.bus.publish("eval.decisions", {
                    "symbol": decision.symbol,
                    "action": decision.action,
                    "qty": decision.qty,
                    "price": decision.price,
                    "ts": ts_ns,
                    "reason": decision.reason,
                })
                inc_decision(decision.action)

    def start(self):
        set_running(not self.ctrl.paused)
        t = threading.Thread(target=self.bus.sub_loop, args=(("md.trades.*","evaluator.controls",), self._on_message, self._stop), daemon=True)
        t.start()
        self._thread = t

    def stop(self):
        self._stop.set()
