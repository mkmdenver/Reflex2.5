from typing import Optional

def _pct_jump(prev: Optional[float], curr: Optional[float]) -> float:
    if prev is None or curr is None or prev == 0:
        return 0.0
    return abs(curr - prev) / abs(prev)

def sane_trade(prev_price: Optional[float], price: float, max_jump: float = 0.2) -> bool:
    if price is None or price <= 0:
        return False
    return _pct_jump(prev_price, price) <= max_jump

def sane_quote(bid: float, ask: float) -> bool:
    if bid is None or ask is None: return False
    if bid <= 0 or ask <= 0: return False
    return bid <= ask
