# evaluator/filters/filter1_float_watch.py
# Seed WATCH tier based on float band (Filter 1).
#
# Usage (from repo root, with .env loaded via _env.bat):
#   filter1_float_watch
#
# Behavior:
#   - Connects to Postgres via common.dbLayer.dbutils.connection()
#   - SELECTs symbols from fundamental_data where shares_float is in range
#   - For each symbol, POSTs to DataHub:
#         POST /v1/tiers/service
#         { "symbol": "...", "tier": "WATCH", "source": "filter1_float_watch" }
#
# This is intentionally a one-shot script for now: run it to “lift” the
# universe of candidates from COLD -> WATCH. Later we can add looping.

from __future__ import annotations

import json
import logging
import os
import sys
from typing import List, Tuple

import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path
# ---------------------------------------------------------------------------

HERE = os.path.dirname(__file__)              # .../evaluator/filters
EVAL_DIR = os.path.dirname(HERE)              # .../evaluator
ROOT = os.path.dirname(EVAL_DIR)              # repo root (Reflex2.3)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common.utils import load_env        # type: ignore
from common.dbLayer import dbutils       # type: ignore

LOG = logging.getLogger("filter1_float_watch")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """
    Load env and return a small config dict.
    """
    load_env()  # be nice if user just runs after _env.bat

    float_min = int(os.getenv("FILTER1_FLOAT_MIN", "1000000"))    # 1M
    float_max = int(os.getenv("FILTER1_FLOAT_MAX", "20000000"))   # 20M

    source = os.getenv("FILTER1_SOURCE", "filter1_float_watch")

    api_base = os.getenv("COCKPIT_DATAHUB_URL")
    if not api_base:
        port = os.getenv("DATAHUB_API_PORT", "7000")
        api_base = f"http://127.0.0.1:{port}"

    return {
        "float_min": float_min,
        "float_max": float_max,
        "source": source,
        "api_base": api_base.rstrip("/"),
    }


# ---------------------------------------------------------------------------
# DB: fetch candidates from fundamental_data
# ---------------------------------------------------------------------------

def fetch_float_candidates(
    float_min: int,
    float_max: int,
) -> List[Tuple[str, int]]:
    """
    Return [(symbol, shares_float), ...] for symbols with float in [min,max].
    Uses the unified DB connection from dbutils.
    """
    conn = dbutils.connection()
    try:
        q = """
            SELECT symbol, shares_float
            FROM fundamental_data
            WHERE shares_float IS NOT NULL
              AND shares_float BETWEEN %s AND %s
            ORDER BY shares_float ASC;
        """
        params = (float_min, float_max)
        with conn.cursor() as cur:
            cur.execute(q, params)
            rows = cur.fetchall()
        return [(r[0], int(r[1])) for r in rows]
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# DataHub: call /v1/tiers/service
# ---------------------------------------------------------------------------

def request_watch_tier(
    api_base: str,
    symbol: str,
    source: str,
) -> None:
    """
    Fire-and-forget request to DataHub to raise symbol to WATCH tier.
    """
    payload = {
        "symbol": symbol.upper(),
        "tier": "WATCH",
        "source": source,
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
            "tier request HTTPError for %s: status=%s body=%r",
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
    source = cfg["source"]
    api_base = cfg["api_base"]

    LOG.info(
        "Filter1 (float) starting: float_min=%d float_max=%d api_base=%s source=%s",
        float_min,
        float_max,
        api_base,
        source,
    )

    try:
        candidates = fetch_float_candidates(float_min, float_max)
    except Exception as e:
        LOG.exception("failed to fetch float candidates: %r", e)
        sys.exit(1)

    LOG.info("found %d symbols in float band [%d, %d]",
             len(candidates), float_min, float_max)

    promoted = 0
    for sym, shares_float in candidates:
        request_watch_tier(api_base, sym, source)
        promoted += 1
        # Tiny log every so often so the console isn't a firehose
        if promoted % 50 == 0:
            LOG.info("promoted %d so far; latest=%s float=%d",
                     promoted, sym, shares_float)

    LOG.info("Filter1 complete: promoted %d symbols into WATCH (requests sent).", promoted)


if __name__ == "__main__":
    main()
