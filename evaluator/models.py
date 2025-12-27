# evaluator/models.py
from __future__ import annotations

from typing import Optional, Literal
from typing_extensions import Annotated
from enum import Enum

from pydantic import BaseModel, Field, ConfigDict, HttpUrl


# ---------- enums (explicit > regex) ----------
class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class TIF(str, Enum):
    DAY = "day"
    GTC = "gtc"


# ---------- common scalar constraints ----------
Symbol = Annotated[
    str,
    Field(min_length=1, max_length=10, pattern=r"^[A-Z.\-]+$")  # adjust if you allow lowercase
]

PositiveQty = Annotated[int, Field(ge=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]
NonNegativeFloat = Annotated[float, Field(ge=0.0)]


# ---------- requests ----------
class OrderIntentRequest(BaseModel):
    """
    Evaluator → Trader: intent to enter/exit a position.
    """
    symbol: Symbol
    side: Side
    qty: PositiveQty
    order_type: OrderType = OrderType.MARKET
    tif: TIF = TIF.DAY
    strategy: str = "na"
    limit_price: Optional[NonNegativeFloat] = None  # required only for limit
    client_tag: Optional[str] = None  # for tracing

    model_config = ConfigDict(extra="forbid")


class CancelOrderRequest(BaseModel):
    symbol: Symbol
    client_order_id: Optional[str] = None
    reason: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class TierChangeRequest(BaseModel):
    """
    For tier/priority commands you mentioned (watch/warm/hot).
    """
    symbol: Symbol
    tier: Literal["COLD", "WATCH", "WARM", "HOT"]

    model_config = ConfigDict(extra="forbid")


class RiskProbeRequest(BaseModel):
    """
    Optional: ask Trader for current risk/cash headroom before deciding to emit an intent.
    """
    symbol: Symbol
    side: Side
    estimated_price: Optional[NonNegativeFloat] = None
    intended_qty: Optional[PositiveQty] = None

    model_config = ConfigDict(extra="forbid")


# ---------- responses ----------
class AckResponse(BaseModel):
    ok: bool = True
    message: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class IntentAcceptedResponse(BaseModel):
    ok: bool = True
    client_order_id: Optional[str] = None
    routed_to: Optional[str] = None  # broker or "sim"
    message: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class HealthResponse(BaseModel):
    instance: str
    mode: str
    evaluator_api: str
    trader_api: Optional[HttpUrl] = None
    uptime_seconds: NonNegativeInt

    model_config = ConfigDict(extra="forbid")
