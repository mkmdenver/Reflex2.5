# common/db_fundamentals_writer.py
"""
Upsert fundamentals into the fundamentals table using the unified DB layer.

Expected columns (normalized):
- symbol, company, exchange, market, market_cap, list_date, sic_code, sic_description
Optional (if you later add them to your schema, just include in COLUMNS and env overrides):
- sector, industry
"""

from __future__ import annotations
from typing import Iterable, Mapping, Sequence, Any, List, Iterator, Optional
from datetime import datetime, date
import os
from typing import Union

from .db import db

# -------------------------
# Table/column config
# -------------------------
TABLE_FUND = os.getenv("REFLEX_TABLE_FUNDAMENTALS", "fundamental_data")

COL_SYMBOL = os.getenv("REFLEX_COL_SYMBOL", "symbol")
COL_COMPANY = os.getenv("REFLEX_COL_COMPANY", "company")
COL_EXCHANGE = os.getenv("REFLEX_COL_EXCHANGE", "exchange")
COL_MARKET = os.getenv("REFLEX_COL_MARKET", "market")
COL_MCAP = os.getenv("REFLEX_COL_MARKET_CAP", "market_cap")
COL_LIST_DATE = os.getenv("REFLEX_COL_LIST_DATE", "list_date")
COL_SIC_CODE = os.getenv("REFLEX_COL_SIC_CODE", "sic_code")
COL_SIC_DESC = os.getenv("REFLEX_COL_SIC_DESCRIPTION", "sic_description")

# If your schema includes these, uncomment or set via env:
COL_SECTOR = os.getenv("REFLEX_COL_SECTOR")          # e.g. "sector"
COL_INDUSTRY = os.getenv("REFLEX_COL_INDUSTRY")      # e.g. "industry"

# Base required columns
COLUMNS: List[str] = [
    COL_SYMBOL, COL_COMPANY, COL_EXCHANGE, COL_MARKET,
    COL_MCAP, COL_LIST_DATE, COL_SIC_CODE, COL_SIC_DESC
]

# Optionally extend with sector/industry if configured
if COL_SECTOR:
    COLUMNS.append(COL_SECTOR)
if COL_INDUSTRY:
    COLUMNS.append(COL_INDUSTRY)


# -------------------------
# Helpers
# -------------------------
def _coerce_list_date(v: Any) -> Optional[date]:
    """Accepts 'YYYY-MM-DD' / datetime / date / None → date or None."""
    if v is None or v == "":
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    # strings like "2020-01-15"
    try:
        return datetime.fromisoformat(str(v)).date()
    except Exception:
        return None


def _coerce_value(col: str, v: Any) -> Any:
    if col == COL_LIST_DATE:
        return _coerce_list_date(v)
    # market_cap may be float or int or string; try numeric
    if col == COL_MCAP and isinstance(v, str):
        try:
            return float(v.replace(",", ""))
        except Exception:
            return None
    return v




def _iter_values(rows: Union[Iterable[Mapping[str, Any]], Iterable[Sequence[Any]]], columns: Sequence[str]) -> Iterator[List[Any]]:
    """
    Accept dict-like rows or sequences aligned to `columns`, coercing values as needed.
    """
    for r in rows:
        if isinstance(r, Mapping):
            yield [_coerce_value(col, r.get(col)) for col in columns]
        else:
            # Sequence: assume already aligned; still coerce where needed
            out: List[Any] = []
            for col, val in zip(columns, r):
                out.append(_coerce_value(col, val))
            # if sequence shorter than columns, pad with None
            while len(out) < len(columns):
                out.append(None)
            yield out


# -------------------------
# Public API
# -------------------------
def upsert_fundamentals(
    rows: Iterable[Mapping[str, Any]] | Iterable[Sequence[Any]],
    *,
    chunk_size: int = 500
) -> int:
    """
    Upsert fundamentals (conflict on symbol, update the rest).
    Returns the attempted row count (see unified layer semantics).
    """
    update_cols = [c for c in COLUMNS if c != COL_SYMBOL]
    return db.bulk_upsert(
        table=TABLE_FUND,
        columns=COLUMNS,
        rows=_iter_values(rows, COLUMNS),
        conflict_cols=[COL_SYMBOL],
        update_cols=update_cols,
        chunk_size=chunk_size,
    )
