import uuid
from datetime import datetime, timezone
from typing import Dict

SIM_ORDERS: Dict[str, dict] = {}
SIM_POSITIONS: Dict[str, dict] = {}

async def get_positions(account_id: str) -> list[dict]:
  return [{"symbol": s, "qty": v.get("qty",0), "avg_entry_price": v.get("avg_price",0.0), "current_price": v.get("last", v.get("avg_price",0.0)), "unrealized_pl": 0.0} for s,v in SIM_POSITIONS.items()]

async def get_orders(account_id: str, status: str = "open") -> list[dict]:
  return list(SIM_ORDERS.values())

async def place_order(
  *, account_id: str, symbol: str, side: str, type: str, qty: float,
  limit_price: float | None, stop_price: float | None, tif: str, extended_hours: bool,
  client_order_id: str | None, bracket: dict | None,
) -> dict:
  oid = client_order_id or str(uuid.uuid4())
  order = {
    "id": oid,
    "status": "accepted",
    "symbol": symbol.upper(),
    "side": side,
    "type": type,
    "qty": qty,
    "limit_price": limit_price,
    "stop_price": stop_price,
    "time_in_force": tif,
    "extended_hours": extended_hours,
    "submitted_at": datetime.now(timezone.utc).isoformat(),
    "order_class": "bracket" if bracket else "simple",
  }
  SIM_ORDERS[oid] = order
  pos = SIM_POSITIONS.get(symbol.upper(), {"qty": 0, "avg_price": limit_price or 0.0, "last": limit_price or 0.0})
  pos["qty"] += qty if side == "buy" else -qty
  SIM_POSITIONS[symbol.upper()] = pos
  return order
