# datahub/subscriptions.py
from common.tiers import Tier, SUBSCRIPTION_POLICY

class SubManager:
    def __init__(self, feed):
        self.feed = feed  # your Polygon adapters
        self.current = {} # symbol -> Tier

    def apply(self, symbol: str, tier: Tier):
        prev = self.current.get(symbol)
        if prev is not None and tier <= prev:
            return  # monotonic up only
        self.current[symbol] = tier
        pol = SUBSCRIPTION_POLICY[tier]
        # Idempotent ops; your feed adapter should no-op on duplicate subscribes
        if pol["minute"]: self.feed.sub_minute(symbol)
        if pol["daily"]:  self.feed.sub_daily(symbol)
        if pol["quotes_tob"]: self.feed.sub_quotes_tob(symbol)
        if pol["trades"]: self.feed.sub_trades(symbol)
        if pol["quotes_l2"]: self.feed.sub_quotes_l2(symbol)
