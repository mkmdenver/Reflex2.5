# evaluator/filters/filter2_cameron_from_ross.py
from __future__ import annotations

"""
Filter 2: simple Ross-style price/volume pillars over the float-universe.

Design (for now):
- Start from the same float band as Filter 1 (fundamental_data.shares_float in [min,max]).
- For each candidate symbol:
    - Look at today's minute_bars to estimate:
        - latest price (last close)
        - intraday volume (sum of today's minute volume)
    - If it passes coarse Ross-style pillars:
        - price in [MIN_PRICE, MAX_PRICE]
        - intraday volume >= MIN_DAY_VOL
    - Then:
        - Raise service tier to WARM via DataHub /v1/tiers/service
        - Publish an event onto eval.ross_pillars

This does *not* subscribe to any upstream stream; it "scans" the universe and
emits the first-generation Ross-style stream, matching the intent that
Filter 2 does not listen, it decides and writes.

For debugging, this version logs every candidate with:
    symbol, price, day_volume, and a "reason" why it failed or "passed".
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone, date
from typing import List, Tuple

import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path
# ---------------------------------------------------------------------------

HERE = os.path.dirname(__file__)              # .../evaluator/filters
EVAL_DIR = os.path.dirname(HERE)              # .../evaluator
ROOT = os.path.dirname(EVAL_DIR)              # repo root
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common.utils import load_env          # type: ignore
from common.dbLayer import dbutils         # type: ignore
from common.bus import publish_sync        # type: ignore

LOG = logging.getLogger("filter2_ross_minute")

ROSS_CHANNEL = "eval.ross_pillars"  # stream this filter will write to


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    load_env()

    float_min = int(os.getenv("FILTER1_FLOAT_MIN", "1000000"))
    float_max = int(os.getenv("FILTER1_FLOAT_MAX", "20000000"))

    min_price = float(os.getenv("ROSS_MIN_PRICE", "2.0"))
    max_price = float(os.getenv("ROSS_MAX_PRICE", "20.0"))
    min_day_vol = int(os.getenv("ROSS_MIN_DAY_VOL", "100000"))

    api_base = os.getenv("COCKPIT_DATAHUB_URL")
    if not api_base:
        port = os.getenv("DATAHUB_API_PORT", "7000")
        api_base = f"http://127.0.0.1:{port}"

    return {
        "float_min": float_min,
        "float_max": float_max,
        "min_price": min_price,
        "max_price": max_price,
        "min_day_vol": min_day_vol,
        "api_base": api_base.rstrip("/"),
    }


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _fetch_float_candidates(conn, float_min: int, float_max: int) -> List[str]:
    """
    Universe: same as Filter 1, but we only need symbols here.
    """
    q = """
        SELECT symbol
        FROM fundamental_data
        WHERE shares_float IS NOT NULL
          AND shares_float BETWEEN %s AND %s
        ORDER BY shares_float ASC;
    """
    with conn.cursor() as cur:
        cur.execute(q, (float_min, float_max))
        rows = cur.fetchall()
    return [r[0].upper() for r in rows]


def _today_utc_start() -> datetime:
    # Midnight UTC today; good enough for intraday aggregation.
    d = date.today()
    return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc)


def _intraday_price_volume(conn, symbol: str) -> Tuple[float, int]:
    """
    Return (last_price, day_volume) for today's minute_bars for symbol.

    Schema:
      minute_bars (
        symbol TEXT,
        timestamp TIMESTAMPTZ,  -- start of minute (UTC)
        open NUMERIC,
        high NUMERIC,
        low  NUMERIC,
        close NUMERIC,
        volume BIGINT,
        ...
      )
    """
    start = _today_utc_start()
    q = """
        SELECT close, volume
        FROM minute_bars
        WHERE symbol = %s
          AND timestamp >= %s
        ORDER BY timestamp ASC;
    """
    with conn.cursor() as cur:
        cur.execute(q, (symbol, start))
        rows = cur.fetchall()

    if not rows:
        return 0.0, 0

    last_close = float(rows[-1][0] or 0.0)
    day_vol = 0
    for _, vol in rows:
        if vol is None:
            continue
        day_vol += int(vol)

    return last_close, day_vol


# ---------------------------------------------------------------------------
# DataHub tier bump
# ---------------------------------------------------------------------------

def _request_warm_tier(api_base: str, symbol: str) -> None:
    payload = {
        "symbol": symbol.upper(),
        "tier": "WARM",
        "source": "filter2_ross_minute",
        "note": "ross_pillars_coarse",
    }
    body = json.dumps(payload).encode("utf-8")
    url = f"{api_base}/v1/tiers/service"

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if resp.status != 200:
                LOG.warning(
                    "tier request non-200 for %s: status=%s",
                    symbol,
                    resp.status,
                )
    except urllib.error.HTTPError as e:
        LOG.warning(
            "tier request HTTPError for %s: status=%s reason=%r",
            symbol,
            getattr(e, "code", None),
            getattr(e, "reason", None),
        )
    except Exception as e:
        LOG.warning("tier request error for %s: %r", symbol, e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
        format="%(asctime)s %(levelname)s:%(name)s: %(message)s",
    )

    cfg = _load_config()
    float_min = cfg["float_min"]
    float_max = cfg["float_max"]
    min_price = cfg["min_price"]
    max_price = cfg["max_price"]
    min_day_vol = cfg["min_day_vol"]
    api_base = cfg["api_base"]

    LOG.info(
        "Filter2 (Ross minute) starting: float_band=[%d,%d] "
        "price=[%.2f,%.2f] min_day_vol=%d api_base=%s",
        float_min, float_max, min_price, max_price, min_day_vol, api_base,
    )

    conn = dbutils.connection()
    try:
        symbols = _fetch_float_candidates(conn, float_min, float_max)
        LOG.info("Filter2 universe size: %d symbols", len(symbols))

        passed = 0
        for sym in symbols:
            price, day_vol = _intraday_price_volume(conn, sym)

            # Default reason
            reason = None

            if price <= 0 and day_vol <= 0:
                reason = "no_data"
            elif price <= 0 or day_vol <= 0:
                reason = "invalid_price_or_volume"
            elif not (min_price <= price <= max_price):
                reason = "price_out_of_band"
            elif day_vol < min_day_vol:
                reason = "volume_below_min"
            else:
                reason = "passed"

            # Log every candidate with its values and reason
            LOG.info(
                "Filter2 candidate: %s price=%.4f day_vol=%d reason=%s",
                sym, price, day_vol, reason,
            )

            if reason != "passed":
                # Skip non-pass candidates
                continue

            # Coarse Ross pillars passed
            passed += 1

            # Raise tier to WARM
            _request_warm_tier(api_base, sym)

            # Publish Ross-style event
            payload = {
                "symbol": sym,
                "price": price,
                "day_volume": day_vol,
                "high_of_day": price,  # placeholder; refine later
                "ts": datetime.now(timezone.utc).timestamp(),
                "reason": "ross_pillars_coarse",
            }
            publish_sync(ROSS_CHANNEL, payload)
            LOG.info(
                "Filter2 PASS: %s price=%.4f vol=%d (event published, WARM requested)",
                sym, price, day_vol,
            )

        LOG.info("Filter2 complete: %d/%d symbols passed coarse Ross pillars",
                 passed, len(symbols))
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
