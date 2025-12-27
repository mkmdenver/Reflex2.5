import httpx
from typing import Optional, Literal
from config import settings

OrderType = Literal["market","limit","stop","stop_limit","trailing"]

def _headers(key: str, secret: str):
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

def _creds_for(account_id: str):
    if account_id == settings.ALPACA_PAPER_ACCOUNT_ID:
        return settings.ALPACA_PAPER_BASE, settings.ALPACA_PAPER_KEY_ID, settings.ALPACA_PAPER_SECRET
    if account_id == settings.ALPACA_LIVE_ACCOUNT_ID:
        return settings.ALPACA_LIVE_BASE, settings.ALPACA_LIVE_KEY_ID, settings.ALPACA_LIVE_SECRET
    raise ValueError("Invalid Alpaca account id")

async def get_positions(account_id: str):
    base, key, sec = _creds_for(account_id)
    url = f"{base}/v2/positions"
    async with httpx.AsyncClient(timeout=15) as cli:
        r = await cli.get(url, headers=_headers(key, sec))
        r.raise_for_status()
        return r.json()

async def get_orders(account_id: str, status: str = "open"):
    base, key, sec = _creds_for(account_id)
    alp_status = {"working":"open","filled":"closed","canceled":"closed","pending":"open"}.get(status, "open")
    url = f"{base}/v2/orders?status={alp_status}&limit=100&nested=true"
    async with httpx.AsyncClient(timeout=15) as cli:
        r = await cli.get(url, headers=_headers(key, sec))
        r.raise_for_status()
        return r.json()

async def place_order(
    *, account_id: str, symbol: str, side: str, type: OrderType, qty: float,
    limit_price: Optional[float], stop_price: Optional[float], tif: str, extended_hours: bool,
    client_order_id: Optional[str], bracket: Optional[dict],
) -> dict:
    base, key, sec = _creds_for(account_id)
    url = f"{base}/v2/orders"
    payload = {
        "symbol": symbol.upper(),
        "side": side,
        "type": type.replace("_","-"),
        "qty": qty,
        "time_in_force": tif,
        "extended_hours": extended_hours,
    }
    if limit_price is not None: payload["limit_price"] = limit_price
    if stop_price is not None: payload["stop_price"] = stop_price
    if bracket and (bracket.get("take_profit") or bracket.get("stop_loss")):
        payload["order_class"] = "bracket"
        if bracket.get("take_profit"):
            payload["take_profit"] = {"limit_price": float(bracket["take_profit"])}
        if bracket.get("stop_loss"):
            sl = {"stop_price": float(bracket["stop_loss"])}
            if bracket.get("stop_limit_offset"):
                sl["limit_price"] = float(bracket["stop_loss"]) - float(bracket["stop_limit_offset"])
            payload["stop_loss"] = sl
    if client_order_id:
        payload["client_order_id"] = client_order_id

    async with httpx.AsyncClient(timeout=15) as cli:
        r = await cli.post(url, headers=_headers(key, sec), json=payload)
        r.raise_for_status()
        return r.json()
