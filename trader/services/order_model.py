# trader/models/order_model.py
# Version: Reflex 2.4 — ORDER_METRICS patch
# Date: 2025-12-19

from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field


class Order(BaseModel):
    id: str
    symbol: str
    qty: int
    side: str
    type: str
    time_in_force: str
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    status: str = "new"
    filled_qty: int = 0
    filled_avg_price: Optional[float] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    submitted_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    canceled_at: Optional[datetime] = None
    rejected_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None
    source: Optional[str] = None  # "manual" or "bot:<name>"
    note: Optional[str] = None

    # NEW: Timing fields for latency metrics
    client_sent_at: Optional[datetime] = None
    broker_submit_at: Optional[datetime] = None
    broker_ack_at: Optional[datetime] = None
    first_fill_at: Optional[datetime] = None

    def compute_metrics(self):
        def ms(a, b):
            if a and b:
                return int((b - a).total_seconds() * 1000)
            return None

        return {
            "submit_to_ack_ms": ms(self.broker_submit_at, self.broker_ack_at),
            "ack_to_first_fill_ms": ms(self.broker_ack_at, self.first_fill_at),
            "submit_to_first_fill_ms": ms(self.broker_submit_at, self.first_fill_at),
            "submit_to_done_ms": ms(self.broker_submit_at, self.filled_at or self.canceled_at or self.rejected_at),
        }
