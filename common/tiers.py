# common/tiers.py
from enum import IntEnum, auto

class Tier(IntEnum):
    COLD  = 0
    WATCH = auto()
    WARM  = auto()
    HOT   = auto()

# Subscription policy strictly per Michael's live rules
# COLD: no live feeds at all (daily snapshot is batch-only)
# WATCH: daily + minute bars; no quotes, no trades
# WARM: WATCH + top-of-book quotes
# HOT: full pipe (trades + quotes + minute + daily). L2 quotes optional.
SUBSCRIPTION_POLICY = {
    Tier.COLD:  {"daily": False, "minute": False, "quotes_tob": False, "trades": False, "quotes_l2": False},
    Tier.WATCH: {"daily": True,  "minute": True,  "quotes_tob": False, "trades": False, "quotes_l2": False},
    Tier.WARM:  {"daily": True,  "minute": True,  "quotes_tob": True,  "trades": False, "quotes_l2": False},
    Tier.HOT:   {"daily": True,  "minute": True,  "quotes_tob": True,  "trades": True,  "quotes_l2": True},
}
