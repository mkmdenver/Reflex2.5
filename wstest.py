
import os
import json
import time
import logging
from websocket import WebSocketApp

KEY = "QiJFJRvmCaea9OGVfp6n2IyYpnHF3qFN"
URL = "wss://socket.polygon.io/stocks"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def on_open(ws):
    if not KEY:
        logger.error("POLY_KEY environment variable not set; closing websocket")
        ws.close()
        return
    ws.send(json.dumps({"action": "auth", "params": KEY}))
    ws.send(json.dumps({"action": "subscribe", "params": "T.AAPL,Q.AAPL"}))
    logger.info("Sent auth and subscribe messages")

def on_message(ws, m):
    # ws parameter is provided by the WebSocketApp callback signature
    print(time.strftime("%H:%M:%S"), "evt", len(m))

if __name__ == "__main__":
    ws = WebSocketApp(URL, on_open=on_open, on_message=on_message)
    ws.run_forever(ping_interval=10, ping_timeout=5)