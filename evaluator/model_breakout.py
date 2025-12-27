# evaluator/model_breakout.py
import time
from common.bus import publisher, CHANNELS, pack
from common.flags import FLAGS

WINDOW = 20     # minutes
MAX_SPREAD_BPS = 8.0
QTY = 200

class BreakoutModel:
    def __init__(self):
        from collections import deque
        self.highs: dict[str, deque] = {}
        self.last_bar_ts: dict[str, float] = {}

    def on_minute(self, ev: dict) -> None:
        sym = ev["symbol"]
        if FLAGS.get(sym, "DO_NOT_TRADE", False):
            return
        hi = ev.get("high") or ev.get("h")
        if hi is None:
            return
        from collections import deque
        dq = self.highs.setdefault(sym, deque(maxlen=WINDOW))
        dq.append(float(hi))
        self.last_bar_ts[sym] = ev.get("bar_ts") or time.time()

    async def on_quote_tob(self, ev: dict) -> None:
        sym = ev["symbol"]
        if FLAGS.get(sym, "DO_NOT_TRADE", False):
            return
        dq = self.highs.get(sym)
        if not dq or len(dq) < WINDOW:
            return
        rolling_high = max(dq)
        bid = float(ev.get("bid") or ev.get("b") or 0)
        ask = float(ev.get("ask") or ev.get("a") or 0)
        if bid <= 0 or ask <= 0:
            return
        mid = (bid + ask) / 2.0
        spread_bps = (ask - bid) / mid * 1e4
        if mid > rolling_high and spread_bps <= MAX_SPREAD_BPS:
            pub = await publisher()
            order = {
                "symbol": sym,
                "side": "BUY",
                "qty": QTY,
                "type": "MKT",
                "t_recv_ns": ev.get("t_recv_ns", time.time_ns()),
                "t_eval_ns": time.time_ns(),
                "model": "breakout_v0",
            }
            await pub.publish(CHANNELS["order"], pack(order))
