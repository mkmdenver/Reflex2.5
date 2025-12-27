import json
import redis

class GarnetClient:
    def __init__(self, url: str = "redis://127.0.0.1:6379"):
        self.r = redis.Redis.from_url(url, decode_responses=True)

    def publish(self, channel: str, message: str):
        return self.r.publish(channel, message)

    def subscribe(self, *patterns):
        p = self.r.pubsub()
        for pat in patterns:
            if "*" in pat:
                p.psubscribe(pat)
            else:
                p.subscribe(pat)
        return p

def garnet_rcli(url: str = "redis://127.0.0.1:6379"):
    return GarnetClient(url)
