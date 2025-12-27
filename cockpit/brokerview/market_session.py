# cockpit/brokerview/market_session.py
# v1.0 — simple US market session helper for BrokerView

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum
from typing import Tuple

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]


NY_TZ = ZoneInfo("America/New_York")


class SessionState(str, Enum):
    PRE = "PRE"
    OPEN = "OPEN"
    POST = "POST"
    CLOSED = "CLOSED"


@dataclass
class SessionInfo:
    state: SessionState
    regular_open: datetime
    regular_close: datetime
    note: str = ""


async def session_state_at(now_et: datetime) -> Tuple[SessionState, datetime, datetime, str]:
    """
    Compute a coarse US equity session state for the given *ET* timestamp.

    Regular session: 09:30–16:00 ET (ignoring holidays for now).
    - PRE:   04:00–09:30
    - OPEN:  09:30–16:00
    - POST:  16:00–20:00
    - CLOSED: everything else
    """
    if now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=NY_TZ)
    else:
        now_et = now_et.astimezone(NY_TZ)

    today = now_et.date()
    reg_open = datetime.combine(today, time(9, 30), tzinfo=NY_TZ)
    reg_close = datetime.combine(today, time(16, 0), tzinfo=NY_TZ)

    pre_start = datetime.combine(today, time(4, 0), tzinfo=NY_TZ)
    post_end = datetime.combine(today, time(20, 0), tzinfo=NY_TZ)

    if pre_start <= now_et < reg_open:
        state = SessionState.PRE
        note = "Pre-market"
    elif reg_open <= now_et < reg_close:
        state = SessionState.OPEN
        note = "Regular session"
    elif reg_close <= now_et < post_end:
        state = SessionState.POST
        note = "After-hours"
    else:
        state = SessionState.CLOSED
        note = "Market closed"

    return state, reg_open, reg_close, note


async def current_session() -> dict:
    """
    Used by:

        @app.get("/v1/market/session")
        async def market_session():
            return await current_session()
    """
    now_et = datetime.now(NY_TZ)
    state, reg_open, reg_close, note = await session_state_at(now_et)
    return {
        "state": state.value,
        "regular_open": reg_open.isoformat(),
        "regular_close": reg_close.isoformat(),
        "server_time": now_et.isoformat(),
        "note": note,
    }
