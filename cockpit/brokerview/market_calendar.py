# cockpit/brokerview/market_calendar.py
# v0.5 – NYSE calendar using pandas-market-calendars only, no Alpaca.
# This module is intentionally thin and stable so other code (market_session)
# can rely on:
#   - get_calendar_window(start, end)
#   - session_bounds_regular(day)
#   - live_session_bounds_regular(day)  (alias)
#   - is_trading_day(day)

from __future__ import annotations

from datetime import date, datetime
from typing import Optional, Tuple

import pandas as pd
import pandas_market_calendars as mcal

_NY_TZ = "America/New_York"


async def get_calendar_window(start: date, end: date) -> pd.DataFrame:
    """
    Return a DataFrame with index=date and columns:
      - market_open (tz-aware, America/New_York)
      - market_close (tz-aware, America/New_York)
    for all NYSE trading sessions between start and end, inclusive.
    """
    nyse = mcal.get_calendar("XNYS")
    sched = nyse.schedule(start_date=start, end_date=end)

    # sched index is a DatetimeIndex; convert to date index for easy lookup.
    sched = sched.copy()
    sched["date"] = sched.index.date
    sched.set_index("date", inplace=True)

    # Normalise / keep only the expected columns.
    cols = {}
    if "market_open" in sched.columns:
        cols["market_open"] = "market_open"
    if "market_close" in sched.columns:
        cols["market_close"] = "market_close"
    sched = sched[list(cols.keys())]

    return sched


async def session_bounds_regular(day: date) -> Optional[Tuple[datetime, datetime]]:
    """
    Return (regular_session_open, regular_session_close) in NY time for the given
    day, or None if the day is not a trading session (weekend/holiday).
    """
    cal = await get_calendar_window(day, day)

    if day not in cal.index:
        # Non-trading day: weekend or holiday
        return None

    row = cal.loc[day]
    open_dt: datetime = row["market_open"].to_pydatetime()
    close_dt: datetime = row["market_close"].to_pydatetime()
    return open_dt, close_dt


# Old name used by market_session.py
live_session_bounds_regular = session_bounds_regular


async def is_trading_day(day: date) -> bool:
    """
    Backwards-compatible helper expected by market_session.py.

    Returns True if `day` is an NYSE trading day (i.e. appears in the calendar
    between day and day), False otherwise.
    """
    cal = await get_calendar_window(day, day)
    return day in cal.index
