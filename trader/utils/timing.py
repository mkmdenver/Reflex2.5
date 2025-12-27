# trader/utils/timing.py
# Version: Reflex 2.4 — ORDER_METRICS patch
# Date: 2025-12-19

from datetime import datetime

def ms_between(start: datetime, end: datetime) -> int:
    if start and end:
        return int((end - start).total_seconds() * 1000)
    return None
