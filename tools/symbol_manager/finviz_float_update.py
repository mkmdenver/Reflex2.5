#!/usr/bin/env python3
"""
Reflex2 — Finviz Float Updater

Fetches:
  - Shs Float  -> fundamental_data.shares_float (BIGINT)
  - Float      -> fundamental_data.float_percent (NUMERIC)

Modes:
  --from-db          -> updates all symbols already in fundamental_data
  --symbols A B C    -> updates provided list
  --symbols-file X   -> updates symbols from a text file (one per line)

Env:
  Loads <repo_root>/.env then <repo_root>/.env.local (does not override existing env vars)
  Uses REFLEX_PG_DSN (fallback DATABASE_URL)

Notes:
  - Finviz has no official API. This is HTML scrape of the quote page.
  - Be polite: use --sleep to avoid rate limits.
"""

from __future__ import annotations

import argparse
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

import psycopg2
from psycopg2.extras import execute_values


# --------------------------------------------------------------------------------------
# Logging (simple, consistent)
# --------------------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------------------
# Repo root + env loading (matches your tool patterns)
# --------------------------------------------------------------------------------------

def _repo_root_from_here() -> Path:
    """
    Resolve repo root from this file location:
      tools/symbol_manager/finviz_float_update.py -> repo root
    """
    here = Path(__file__).resolve()
    return here.parents[2]


def _load_env_file(path: Path) -> None:
    """
    Minimal .env loader: KEY=VALUE lines.
    - Ignores blank lines, comments (# or ;)
    - Ignores section headers like [core]
    - Does NOT override existing env vars
    """
    if not path.exists():
        return

    log(f'[ENV] Loading "{path}" ...')
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#") or line.startswith(";"):
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
        raise SystemExit(f'[ERROR] Missing required env file: "{env}"')

    _load_env_file(env)

    if env_local.exists():
        _load_env_file(env_local)

    return root


# --------------------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------------------

_NUM_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([KMBT]?)\s*$", re.IGNORECASE)


def parse_compact_number(s: str) -> Optional[int]:
    """
    Parses compact numbers like '12.3M', '900K', '1.2B' into an int.
    """
    if not s:
        return None
    s = s.replace(",", "").strip()
    m = _NUM_RE.match(s)
    if not m:
        return None
    val = float(m.group(1))
    suf = m.group(2).upper()
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000, "T": 1_000_000_000_000}.get(suf)
    if mult is None:
        return None
    return int(round(val * mult))


def parse_percent(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.strip().replace("%", "")
    try:
        return float(s)
    except ValueError:
        return None


# --------------------------------------------------------------------------------------
# Finviz scrape
# --------------------------------------------------------------------------------------

def fetch_finviz_snapshot(symbol: str, session: requests.Session, timeout: int = 20) -> Dict[str, str]:
    url = f"https://finviz.com/quote.ashx?t={symbol.upper()}"
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", class_="snapshot-table2")
    if not table:
        raise RuntimeError("Finviz snapshot table not found (class snapshot-table2).")

    cells = [c.get_text(strip=True) for c in table.find_all("td")]
    out: Dict[str, str] = {}
    for i in range(0, len(cells) - 1, 2):
        label = cells[i]
        value = cells[i + 1]
        if label and value:
            out[label] = value
    return out


from finvizfinance.quote import finvizfinance

def finviz_float(symbol: str, session=None):
    q = finvizfinance(symbol)
    d = q.ticker_fundament()  # dict of snapshot fields
    shs = parse_compact_number(d.get("Shs Float", "") or d.get("Shares Float", ""))
    pct = parse_percent(d.get("Float", "") or d.get("Float %", ""))
    return shs, pct



# --------------------------------------------------------------------------------------
# DB
# --------------------------------------------------------------------------------------

def db_connect():
    dsn = os.environ.get("REFLEX_PG_DSN") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("[ERROR] Missing REFLEX_PG_DSN (or DATABASE_URL) after env load.")
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


def upsert_floats(rows: List[Tuple[str, Optional[int], Optional[float]]]) -> int:
    if not rows:
        return 0

    sql = """
    INSERT INTO fundamental_data (symbol, shares_float, float_percent, last_updated)
    VALUES %s
    ON CONFLICT (symbol) DO UPDATE SET
        shares_float = EXCLUDED.shares_float,
        float_percent = EXCLUDED.float_percent,
        last_updated = NOW()
    """

    with db_connect() as conn:
        with conn.cursor() as cur:
            execute_values(cur, sql, rows)
        conn.commit()
    return len(rows)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

def _dedupe_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="finviz_float_update",
        description="Update public float (shares + percent) in fundamental_data from Finviz quote pages.",
    )
    ap.add_argument("--from-db", action="store_true", help="Update all symbols currently in fundamental_data")
    ap.add_argument("--symbols", nargs="+", help="Symbols to update (e.g. --symbols BNAI SPY TSLA)")
    ap.add_argument("--symbols-file", help="Text file with one symbol per line")
    ap.add_argument("--sleep", type=float, default=1.25, help="Seconds between requests")
    args = ap.parse_args()

    root = load_root_env()
    log(f"[PATH] ROOT={root}")

    symbols: List[str] = []

    if args.from_db:
        symbols.extend(symbols_from_db())

    if args.symbols:
        symbols.extend([s.strip().upper() for s in args.symbols if s and s.strip()])

    if args.symbols_file:
        p = Path(args.symbols_file)
        if not p.exists():
            raise SystemExit(f'[ERROR] symbols-file not found: "{p}"')
        symbols.extend([ln.strip().upper() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()])

    symbols = _dedupe_keep_order([s for s in symbols if s])
    if not symbols:
        raise SystemExit("No symbols provided. Use --from-db, --symbols, or --symbols-file.")

    sess = requests.Session()
    sess.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://finviz.com/",
        }
    )

    up_rows: List[Tuple[str, Optional[int], Optional[float]]] = []
    for i, sym in enumerate(symbols, 1):
        try:
            shs, pct = finviz_float(sym, sess)
            up_rows.append((sym, shs, pct))
            log(f"[{i}/{len(symbols)}] {sym}: Shs Float={shs}  Float%={pct}")
        except Exception as e:
            log(f"[{i}/{len(symbols)}] {sym}: ERROR: {e}")
        time.sleep(args.sleep)

    n = upsert_floats(up_rows)
    log(f"[DONE] Upserted {n} row(s) into fundamental_data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
