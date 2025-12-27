# trader/logger/flight_recorder.py
# Version: Reflex 2.4 — ORDER_METRICS patch
# Date: 2025-12-19

import os
import json
from datetime import datetime
from trader.models.order_model import Order

LOG_DIR = os.path.join("logs", "trader")
os.makedirs(LOG_DIR, exist_ok=True)

def record_order_event(order: Order, event: str):
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    log_path = os.path.join(LOG_DIR, f"{date_str}.jsonl")

    payload = {
        "ts": datetime.utcnow().isoformat(),
        "event": event,
        "order_id": order.id,
        "symbol": order.symbol,
        "initiator": order.source or "unknown",
        "status": order.status,
        "filled_qty": order.filled_qty,
        "avg_price": order.filled_avg_price,
        "times": {
            "client_sent_at": order.client_sent_at,
            "broker_submit_at": order.broker_submit_at,
            "broker_ack_at": order.broker_ack_at,
            "first_fill_at": order.first_fill_at,
            "filled_at": order.filled_at,
            "canceled_at": order.canceled_at,
            "rejected_at": order.rejected_at,
        },
        "metrics": order.compute_metrics(),
    }

    with open(log_path, "a") as f:
        f.write(json.dumps(payload, default=str) + "\n")
