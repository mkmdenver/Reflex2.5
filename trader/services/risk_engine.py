# trader/risk_engine.py
"""
Risk engine (minimal, extensible).

Enforced now:
- Basic intent shape (symbol/side/order_type, qty > 0)
- PDT restriction (if applies)
- Cash vs Buying Power (IRA/CASH require cash; MARGIN uses buying_power) when a price is known
- Short permission (allow_short flag)
- Ladder guard: max_net_qty (blocks if next_net would exceed)

Notes:
- If no usable price is provided (e.g., market without estimate), the funds check
  is skipped but other checks still apply. Upstream can provide an estimated price
  via 'price' or 'limit_price' to enable funds checking.
"""
from typing import Dict, Any, Tuple


def _norm_str(x: Any) -> str:
    return (str(x or "")).strip().lower()


def _as_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _cost(intent: Dict[str, Any]) -> float:
    """
    Use limit_price first (most conservative for BUY), else fall back to price.
    If neither present or not numeric, return 0.0 (skip funds check).
    """
    qty = float(intent.get("qty", 0))
    # prefer limit_price, then price
    price = intent.get("limit_price")
    if price in (None, "", 0, 0.0):
        price = intent.get("price")
    try:
        price = float(price)
    except Exception:
        price = 0.0
    if price <= 0 or qty <= 0:
        return 0.0
    return qty * price


def decide(intent: Dict[str, Any], account: Dict[str, Any], net_qty_now: int) -> Tuple[bool, str]:
    # ---- shape checks -------------------------------------------------------
    symbol = intent.get("symbol")
    side = _norm_str(intent.get("side"))
    otype = _norm_str(intent.get("order_type"))
    qty = _as_int(intent.get("qty"), 0)

    if not symbol or side not in ("buy", "sell") or otype not in ("market", "limit", "stop", "stop_limit"):
        return False, "malformed_intent"

    if qty <= 0:
        return False, "invalid_qty"

    # ---- account properties -------------------------------------------------
    acct_class = _norm_str(account.get("class") or "margin")
    pdt_applies = bool(account.get("pdt_applies", True))
    pdt_restricted = bool(account.get("pdt_restricted", False))
    allow_short = bool(account.get("allow_short", True))
    is_margin = (acct_class == "margin")
    is_ira_or_cash = (acct_class in ("ira", "cash"))

    # PDT gate
    if pdt_applies and pdt_restricted:
        return False, "pdt_restricted"

    # ---- funds check (only if we can compute a cost) ------------------------
    est_cost = _cost(intent)
    if est_cost > 0.0:
        buying_power = float(account.get("buying_power") or 0.0)
        cash = float(account.get("cash") or 0.0)
        if is_ira_or_cash:
            if est_cost > cash:
                return False, "insufficient_cash"
        else:
            # margin path
            if est_cost > buying_power:
                return False, "insufficient_buying_power"

    # ---- shorts --------------------------------------------------------------
    pi = (str(intent.get("position_intent") or "")).upper()
    if pi in ("SHORT_OPEN", "SHORT_ADD") and not allow_short:
        return False, "short_not_allowed"

    # ---- ladder / max net ----------------------------------------------------
    ladder = intent.get("ladder") or {}
    max_net = ladder.get("max_net_qty")
    if max_net is not None:
        try:
            max_net_i = int(max_net)
            signed = qty if side == "buy" else -qty
            next_net = net_qty_now + signed
            if abs(next_net) > abs(max_net_i):
                return False, "exceeds_max_net_qty"
        except Exception:
            # ignore malformed ladder values
            pass

    # Spread/slippage/“weather” checks can plug in here once telemetry is available.
    return True, "ok"
