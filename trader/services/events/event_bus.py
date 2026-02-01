# trader/events/event_bus.py
# Version: Reflex 2.4 — ORDER_METRICS patch
# Date: 2025-12-19

import json
import asyncio
from enum import Enum
from datetime import datetime
from fastapi import Request
from starlette.responses import EventSourceResponse

subscribers = []

class EventType(str, Enum):
    ORDER_REQUEST = "ORDER_REQUEST"
    ORDER_PLACED = "ORDER_PLACED"
    ORDER_REJECTED = "ORDER_REJECTED"
    BROKER_UPDATE = "BROKER_UPDATE"
    ORDER_METRICS = "ORDER_METRICS"
    FLATTEN_ALL = "FLATTEN_ALL"
    CANCEL_ALL = "CANCEL_ALL"

async def subscribe(request: Request):
    queue = asyncio.Queue()
    subscribers.append(queue)

    async def event_generator():
        try:
            while True:
                data = await queue.get()
                yield {
                    "event": data["type"],
                    "data": json.dumps(data["payload"], default=str)
                }
        except asyncio.CancelledError:
            pass
        finally:
            subscribers.remove(queue)

    return EventSourceResponse(event_generator())

def emit_event(event_type: EventType, payload):
    message = {
        "type": event_type.value,
        "payload": payload
    }
    for queue in subscribers:
        queue.put_nowait(message)
