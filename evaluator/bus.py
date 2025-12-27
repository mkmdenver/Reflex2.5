
from __future__ import annotations
import json, time, threading
from typing import Optional, Iterable
import redis

class Bus:
    def __init__(self, url: str, decode_responses: bool=False):
        self.url = url
        self.r = redis.Redis.from_url(url, decode_responses=decode_responses, socket_timeout=5)

    def publish(self, channel: str, message: dict) -> None:
        try:
            self.r.publish(channel, json.dumps(message))
        except Exception:
            pass

    def sub_loop(self, patterns: Iterable[str], cb, stop_event: threading.Event):
        p = self.r.pubsub(ignore_subscribe_messages=True)
        p.psubscribe(*patterns)
        while not stop_event.is_set():
            try:
                msg = p.get_message(timeout=1.0)
                if not msg: continue
                typ = msg.get("type")
                if typ not in ("message","pmessage"): continue
                ch = msg.get("channel")
                data = msg.get("data")
                if isinstance(ch, bytes): ch = ch.decode("utf-8","ignore")
                if isinstance(data, bytes): data = data.decode("utf-8","ignore")
                try:
                    cb(ch, data)
                except Exception:
                    continue
            except Exception:
                time.sleep(0.1)
        try:
            p.close()
        except Exception:
            pass
