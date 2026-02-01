#!/usr/bin/env python3
"""
Polygon fundamentals updater (Ticker Overview only).

Uses:
  GET /v3/reference/tickers/{ticker}

Writes to:
  fundamental_data(symbol PK, company, sector, industry, country, exchange,
                   market_cap, pe_ratio, shares_float, float_percent,
                   insider_transactions, short_float, average_true_range,
                   last_updated)

Notes:
- We intentionally DO NOT call /vX/reference/financials because it can produce
  confusing duplicate period records. (We can add it later if needed.)
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import psycopg2
from psycopg2.extras import execute_values


def log(msg: str) -> None:
    print(msg, flush=True)


# -------------------------
# env load (ROOT .env + .env.local)
# -------------------------

def _repo_root_from_here() -> Path:
    # tools/symbol_manager/polygon_fundamentals_update.py -> ROOT
    return Path(__file__).resolve().parents[2]


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    log(f'[ENV] Loading "{path}" ...')
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and (k not in os.environ):
            os.environ[k] = v


def load_root_env() -> Path:
    root = _repo_root_from_here()
    env = root / ".env"
    env_local = root / ".env.local"

    if not env.exists():
        raise SystemExit(f'[ERROR] Missing "{env}"')

    _load_env_file(env)
    if env_local.exists():
        _load_env_file(env_local)

    return root


# -------------------------
# DB
# -------------------------

def db_connect():
    dsn = os.environ.get("REFLEX_PG_DSN") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("[ERROR] Missing REFLEX_PG_DSN (or DATABASE_URL)")
    return psycopg2.connect(dsn)


def symbols_from_db() -> List[str]:
    sql = """
        SELECT symbol
        FROM fundamental_data
        WHERE symbol IS NOT NULL AND symbol <> ''
        ORDER BY symbol
    """
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    return [str(r[0]).strip().upper() for r in rows if r and r[0] and str(r[0]).strip()]


def upsert_fundamental_rows(
    rows: List[Tuple[str, Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[float], int]]
) -> int:
    """
    rows: (symbol, company, sector, industry, country, exchange, market_cap, shares_float)
    shares_float is always int (0 if unknown)
    """
    if not rows:
        return 0

    sql = """
    INSERT INTO fundamental_data (
        symbol, company, sector, industry, country, exchange, market_cap, shares_float, last_updated
    )
    VALUES %s
    ON CONFLICT (symbol) DO UPDATE SET
        company = EXCLUDED.company,
        sector = EXCLUDED.sector,
        industry = EXCLUDED.industry,
        country = EXCLUDED.country,
        exchange = EXCLUDED.exchange,
        market_cap = EXCLUDED.market_cap,
        shares_float = EXCLUDED.shares_float,
        last_updated = NOW()
    """

    # IMPORTANT: provide a VALUES template with NOW() as the 9th expression
    template = "(%s,%s,%s,%s,%s,%s,%s,%s,NOW())"

    with db_connect() as conn:
        with conn.cursor() as cur:
            execute_values(cur, sql, rows, template=template)
        conn.commit()

    return len(rows)



# -------------------------
# Polygon
# -------------------------

def polygon_key() -> str:
    key = (
        os.environ.get("POLYGON_API_KEY")
        or os.environ.get("REFLEX_POLYGON_API_KEY")
        or os.environ.get("POLYGON_KEY")
    )
    if not key:
        raise SystemExit("[ERROR] Missing POLYGON_API_KEY (or REFLEX_POLYGON_API_KEY)")
    return key


def polygon_ticker_overview(ticker: str, session: requests.Session, api_key: str, timeout: int = 20) -> Dict[str, Any]:
    url = f"https://api.polygon.io/v3/reference/tickers/{ticker}"
    params = {"apiKey": api_key}
    r = session.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _as_str(x: Any) -> Optional[str]:
    if x is None:
        return None
    s = str(x).strip()
    return s if s else None


def _as_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None


def _as_int(x: Any) -> Optional[int]:
    if x is None:
        return None
    try:
        return int(float(x))
    except Exception:
        return None


def map_overview_to_row(
    ticker: str, payload: Dict[str, Any]
) -> Tuple[str, Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[float], int]:
    """
    Best-effort mapping from /v3/reference/tickers/{ticker} -> fundamental_data subset.
    """
    res = payload.get("results") or {}

    company = _as_str(res.get("name"))
    exchange = _as_str(res.get("primary_exchange"))
    market_cap = _as_float(res.get("market_cap"))

    country = _as_str(res.get("locale"))

    sic_desc = _as_str(res.get("sic_description"))
    industry = sic_desc
    sector = None  # not directly provided cleanly here

    # Prefer weighted shares outstanding when available.
    sh_weighted = _as_int(res.get("weighted_shares_outstanding"))
    sh_class = _as_int(res.get("share_class_shares_outstanding"))

    shares_float = sh_weighted if sh_weighted is not None else sh_class
    if shares_float is None:
        shares_float = 0

    return (ticker, company, sector, industry, country, exchange, market_cap, shares_float)


# -------------------------
# CLI
# -------------------------

def dedupe_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-db", action="store_true", help="Update all symbols already in fundamental_data")
    ap.add_argument("--symbols", nargs="+", help="Specific symbols to update")
    ap.add_argument("--sleep", type=float, default=0.25, help="Delay between API calls")
    args = ap.parse_args()

    root = load_root_env()
    log(f"[PATH] ROOT={root}")

    api_key = polygon_key()

    symbols: List[str] = []
    if args.from_db:
        symbols.extend(symbols_from_db())
    if args.symbols:
        symbols.extend([s.strip().upper() for s in args.symbols if s and s.strip()])

    symbols = dedupe_keep_order([s for s in symbols if s])
    if not symbols:
        raise SystemExit("No symbols. Use --from-db or --symbols.")

    sess = requests.Session()

    batch: List[Tuple[str, Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[float], int]] = []
    ok = 0
    err = 0
    skip = 0

    for i, sym in enumerate(symbols, 1):
        try:
            payload = polygon_ticker_overview(sym, sess, api_key)
            row = map_overview_to_row(sym, payload)
            batch.append(row)
            ok += 1
            log(f"[{i}/{len(symbols)}] {sym}: market_cap={row[6]} shares_out={row[7]}")
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status == 404:
                skip += 1
                log(f"[{i}/{len(symbols)}] {sym}: SKIP (404 not found)")
                continue
            err += 1
            log(f"[{i}/{len(symbols)}] {sym}: ERROR: {e}")
        except Exception as e:
            err += 1
            log(f"[{i}/{len(symbols)}] {sym}: ERROR: {e}")

        time.sleep(args.sleep)

    n = upsert_fundamental_rows(batch)
    log(f"[DONE] ok={ok} skip={skip} err={err} upserted={n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
