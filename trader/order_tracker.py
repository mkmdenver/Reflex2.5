# trader/core/order_tracker.py
# Version: Reflex 2.4 — ORDER_METRICS patch
# Date: 2025-12-19

from datetime import datetime
from trader.models.order_model import Order
from trader.events.event_bus import emit_event, EventType
from trader.logger.flight_recorder import record_order_event

class OrderTracker:
    def __init__(self):
        self.orders = {}

    def new_order(self, order: Order):
        now = datetime.utcnow()
        order.client_sent_at = now
        self.orders[order.id] = order
        emit_event(EventType.ORDER_REQUEST, order)
        self._emit_metrics(order)
        record_order_event(order, "new_order")

    def submitted_to_broker(self, order_id: str):
        order = self.orders.get(order_id)
        if order:
            order.broker_submit_at = datetime.utcnow()
            emit_event(EventType.ORDER_PLACED, order)
            self._emit_metrics(order)
            record_order_event(order, "submitted_to_broker")

    def broker_acknowledged(self, order_id: str):
        order = self.orders.get(order_id)
        if order:
            order.broker_ack_at = datetime.utcnow()
            emit_event(EventType.BROKER_UPDATE, order)
            self._emit_metrics(order)
            record_order_event(order, "broker_acknowledged")

    def order_filled(self, order_id: str, qty: int, price: float, final=False):
        order = self.orders.get(order_id)
        if order:
            if not order.first_fill_at:
                order.first_fill_at = datetime.utcnow()
            order.filled_qty += qty
            order.filled_avg_price = price  # assume we update average as part of adapter
            if final:
                order.filled_at = datetime.utcnow()
            emit_event(EventType.BROKER_UPDATE, order)
            self._emit_metrics(order)
            record_order_event(order, "order_filled")

    def order_canceled(self, order_id: str):
        order = self.orders.get(order_id)
        if order:
            order.canceled_at = datetime.utcnow()
            emit_event(EventType.BROKER_UPDATE, order)
            self._emit_metrics(order)
            record_order_event(order, "order_canceled")

    def order_rejected(self, order_id: str):
        order = self.orders.get(order_id)
        if order:
            order.rejected_at = datetime.utcnow()
            emit_event(EventType.BROKER_UPDATE, order)
            self._emit_metrics(order)
            record_order_event(order, "order_rejected")

    def _emit_metrics(self, order: Order):
        emit_event(EventType.ORDER_METRICS, {
            "order_id": order.id,
            "symbol": order.symbol,
            "initiator": order.source or "unknown",
            "times": {
                "sent": order.client_sent_at,
                "submit": order.broker_submit_at,
                "ack": order.broker_ack_at,
                "first_fill": order.first_fill_at,
                "filled": order.filled_at,
                "canceled": order.canceled_at,
                "rejected": order.rejected_at,
            },
            "metrics": order.compute_metrics()
        })
