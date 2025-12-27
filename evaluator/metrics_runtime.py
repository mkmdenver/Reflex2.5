
from __future__ import annotations
from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST

registry = CollectorRegistry()

# Gauges / Counters surfaced to cockpit
eval_pnl        = Gauge("eval_pnl", "Evaluator PnL (USD)", registry=registry)
eval_drawdown   = Gauge("eval_drawdown", "Evaluator drawdown (%)", registry=registry)
eval_state      = Gauge("eval_state", "Evaluator running state (0=paused,1=running)", registry=registry)
decisions_total = Counter("eval_decisions_total", "Total decisions made", ["action"], registry=registry)
trades_seen     = Counter("eval_trades_seen_total", "Trade messages seen", registry=registry)
control_updates = Counter("eval_control_updates_total", "Control updates applied", registry=registry)

def set_running(is_running: bool): eval_state.set(1 if is_running else 0)
def set_pnl(pnl: float):           eval_pnl.set(float(pnl))
def set_drawdown(dd_pct: float):   eval_drawdown.set(float(dd_pct))
def inc_decision(action: str, inc: float = 1.0): decisions_total.labels(action=action).inc(inc)
