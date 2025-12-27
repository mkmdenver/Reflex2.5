# cockpit/brokerview/models.py
# v0.6 – Pydantic models for BrokerView API responses

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Market session
# ---------------------------------------------------------------------------


class SessionState(str, Enum):
    CLOSED = "closed"
    PREMARKET = "premarket"
    REGULAR = "regular"
    POSTMARKET = "postmarket"


class MarketSessionDoc(BaseModel):
    """
    Shape returned by /v1/market/session.
    """

    state: SessionState
    is_market_day: bool = False

    now: datetime

    pre_open: Optional[datetime] = None
    regular_open: Optional[datetime] = None
    regular_close: Optional[datetime] = None
    post_close: Optional[datetime] = None

    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


PositionSide = Literal["long", "short"]


class PositionDoc(BaseModel):
    """
    Normalized view of a position as seen by BrokerView.
    """

    account_id: str
    symbol: str

    qty: float = 0.0
    avg_price: float = 0.0

    market_price: Optional[float] = None
    market_value: Optional[float] = None

    unrealized_pl: Optional[float] = None
    unrealized_plpc: Optional[float] = None
    realized_pl: Optional[float] = None

    # some feeds may send this as null / missing when flat
    side: Optional[PositionSide] = None

    # Optional raw broker payload for debugging / inspection.
    raw: Optional[Dict[str, Any]] = None


class PositionsResponse(BaseModel):
    positions: List[PositionDoc] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


class OrderDoc(BaseModel):
    """
    Normalized view of an order as exposed by BrokerView.
    """

    account_id: str
    symbol: str

    side: Optional[str] = None          # e.g. "buy", "sell", "sell_short", ...
    status: Optional[str] = None        # e.g. "new", "filled", "canceled", ...
    type: Optional[str] = None          # e.g. "market", "limit", "stop"

    qty: Optional[float] = None
    filled_qty: Optional[float] = None

    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    time_in_force: Optional[str] = None

    submitted_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    broker_order_id: Optional[str] = None
    client_order_id: Optional[str] = None

    raw: Optional[Dict[str, Any]] = None


class OrdersResponse(BaseModel):
    """
    Response from /v1/orders endpoint.
    """

    orders: List[OrderDoc] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Accounts – UI-facing
# ---------------------------------------------------------------------------


class AccountSummary(BaseModel):
    """
    Single account row in the "Accounts" page.

    Built from Trader's /v1/portfolio/overview output.
    """

    account_id: str

    # Human-friendly label, e.g. "Alpaca Live", "Sim Margin"
    label: str

    # Broker + structural info
    broker: str               # e.g. "alpaca", "sim"
    account_type: str         # e.g. "margin", "cash", "paper"
    status: str = "unknown"   # e.g. "ACTIVE", "DISABLED", "SIM"
    currency: str = "USD"

    # Key balances
    cash: float = 0.0
    equity: float = 0.0
    buying_power: float = 0.0

    updated_at: Optional[datetime] = None


class AccountsResponse(BaseModel):
    accounts: List[AccountSummary] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Accounts – helper types for parsing Trader overview
# ---------------------------------------------------------------------------


class AccountBalances(BaseModel):
    cash: float = 0.0
    equity: float = 0.0
    buying_power: float = 0.0
    updated_at: Optional[datetime] = None


class AccountOverviewEntry(BaseModel):
    balances: AccountBalances = Field(default_factory=AccountBalances)
    positions: List[Any] = Field(default_factory=list)
    open_orders: List[Any] = Field(default_factory=list)


class PortfolioOverview(BaseModel):
    """
    Mirrors Trader's /v1/portfolio/overview payload closely.
    """

    overview: Dict[str, AccountOverviewEntry] = Field(default_factory=dict)
    count: int = 0
    live_only: bool = False
    account_id: Optional[str] = None
