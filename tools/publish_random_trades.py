import json, random, time
from typing import List
from common.comm_garnet.garnet_rcli import garnet_rcli

class SimFeed:
    def __init__(self, url: str = "redis://127.0.0.1:6379", symbols: List[str] = None):
        self.url = url
        self.symbols = symbols or ["AAPL", "MSFT", "TSLA"]

    def start(self, loops: int = 200):
        bus = garnet_rcli(self.url)
        price = {s: 100.0 + random.random() * 10 for s in self.symbols}
        for _ in range(loops):
            for s in self.symbols:
                price[s] *= (1 + random.uniform(-0.001, 0.001))
                trade = {
                    "type": "trade",
                    "symbol": s,
                    "price": round(price[s], 2),
                    "size": random.randint(1, 500),
                    "sip_timestamp": int(time.time_ns()),
                }
                bus.publish(f"md.trades.{s}", json.dumps(trade))
            time.sleep(0.01)
