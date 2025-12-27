# common/db_access.py
from __future__ import annotations
from datetime import datetime
from typing import List
from .dbutils import fetch_all
from .schema import T_MINUTE, T_DAILY, T_TICKS, T_QUOTES, MinuteBar, DailyBar, Tick, Quote

def get_minute_bars(symbol: str, start: datetime, end: datetime, limit: int | None = None) -> List[MinuteBar]:
    q = f"""
        SELECT symbol, ts_utc, open, high, low, close, volume, vwap, trades
        FROM {T_MINUTE}
        WHERE symbol=%s AND ts_utc BETWEEN %s AND %s
        ORDER BY ts_utc ASC
        { 'LIMIT %s' if limit else '' }
    """
    params = [symbol, start, end]
    if limit:
        params.append(limit)
    rows = fetch_all(q, params)
    return [MinuteBar(**r) for r in rows]

def get_daily_bars(symbol: str, start: datetime, end: datetime) -> List[DailyBar]:
    q = f"""
        SELECT symbol, session_date, open, high, low, close, volume
        FROM {T_DAILY}
        WHERE symbol=%s AND session_date BETWEEN %s AND %s
        ORDER BY session_date ASC
    """
    rows = fetch_all(q, [symbol, start, end])
    return [DailyBar(**r) for r in rows]

def get_ticks(symbol: str, start: datetime, end: datetime, limit: int | None = None) -> List[Tick]:
    q = f"""
        SELECT symbol, ts_utc, price, size, exchange
        FROM {T_TICKS}
        WHERE symbol=%s AND ts_utc BETWEEN %s AND %s
        ORDER BY ts_utc ASC
        { 'LIMIT %s' if limit else '' }
    """
    params = [symbol, start, end]
    if limit:
        params.append(limit)
    rows = fetch_all(q, params)
    return [Tick(**r) for r in rows]

def get_quotes(symbol: str, start: datetime, end: datetime, limit: int | None = None) -> List[Quote]:
    q = f"""
        SELECT symbol, ts_utc, bid, ask, bid_size, ask_size, exchange
        FROM {T_QUOTES}
        WHERE symbol=%s AND ts_utc BETWEEN %s AND %s
        ORDER BY ts_utc ASC
        { 'LIMIT %s' if limit else '' }
    """
    params = [symbol, start, end]
    if limit:
        params.append(limit)
    rows = fetch_all(q, params)
    return [Quote(**r) for r in rows]
