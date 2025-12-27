# common/schema.py
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

# ------------------------------------------------------------------
# Table name constants (defaults match your Timescale schema)
# Override via env in db_writer if you need to.
# ------------------------------------------------------------------
T_MINUTE = "minute_bars"
T_DAILY  = "daily_bars"
T_TICKS  = "tick_data"
T_QUOTES = "quote_data"

# ------------------------------------------------------------------
# Row dataclasses used by db_reader (optional but convenient)
# Aligns with unified_polygon + writers’ normalized columns
# ------------------------------------------------------------------

@dataclass(frozen=True)
class MinuteBar:
    symbol: str
    ts_utc: datetime
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None
    vwap: Optional[float] = None
    trades: Optional[int] = None

@dataclass(frozen=True)
class DailyBar:
    symbol: str
    ts_utc: datetime
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None
    vwap: Optional[float] = None
    trades: Optional[int] = None

@dataclass(frozen=True)
class Tick:
    symbol: str
    ts_utc: datetime
    price: float
    size: Optional[float] = None
    exchange: Optional[int] = None
    conditions: Optional[Sequence[int]] = None
    tape: Optional[str] = None
    participant_id: Optional[str] = None

@dataclass(frozen=True)
class Quote:
    symbol: str
    ts_utc: datetime
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    exchange: Optional[int] = None
