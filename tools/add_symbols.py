"""
Add symbols to DB + fetch Finviz fundamentals.

Writes:
  - symbol_metadata (tier/list_date/filters)
  - fundamental_data (company/sector/industry/country/exchange/market_cap/pe/float/atr/etc.)

Schema reference:
  fundamental_data + symbol_metadata are defined in schema.sql
"""

from __future__ import annotations

import argparse
import os
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Dict, Iterable, Optional, Sequence, Tuple

import psycopg

# Prefer requests+bs4; if you already use finvizfinance you can swap later.
import requests
from bs4 import BeautifulSoup


# -----------------------------
# Env / helpers
# -----------------------------

def get_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return v


def chunked(seq: Sequence[str], n: int) -> Iterable[Sequence[str]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def norm_symbol(s: str) -> str:
    return s.strip().upper()


def parse_suffix_number(s: str) -> Optional[float]:
    """
    Parse numbers like:
      1.23B, 450.6M, 12.1K, 123, 123.45
    Returns float (NOT scaled to int unless you want it).
    """
    if s is None:
        return None
    t = s.strip().upper().replace(",", "")
    if t in ("", "-", "N/A"):
        return None

    m = re.match(r"^(-?\d+(?:\.\d+)?)([KMBT])?$", t)
    if not m:
        return None

    val = float(m.group(1))
    suf = m.group(2)
    mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}.get(suf, 1.0)
    return val * mult


def parse_percent(s: str) -> Optional[float]:
    if s is None:
        return None
    t = s.strip().replace("%", "")
    if t in ("", "-", "N/A"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def parse_float_shares(s: str) -> Optional[int]:
    """
    Finviz Float often comes as:
      12.34M, 980K, 1.2B
    Convert to integer shares.
    """
    v = parse_suffix_number(s)
    if v is None:
        return None
    return int(round(v))


def parse_decimal(s: str) -> Optional[float]:
    if s is None:
        return None
    t = s.strip().replace(",", "")
    if t in ("", "-", "N/A"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


# -----------------------------
# Finviz parsing
# -----------------------------

@dataclass
class FinvizFundamentals:
    symbol: str
    company: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    country: Optional[str] = None
    exchange: Optional[str] = None
    market_cap: Optional[float] = None
    pe_ratio: Optional[float] = None
    shares_float: Optional[int] = None
    float_percent: Optional[float] = None
    insider_transactions: Optional[float] = None
    short_float: Optional[float] = None
    average_true_range: Optional[float] = None


FINVIZ_QUOTE_URL = "https://finviz.com/quote.ashx?t={sym}"


def fetch_finviz_html(symbol: str, session: requests.Session) -> str:
    url = FINVIZ_QUOTE_URL.format(sym=symbol)
    # Finviz blocks “default bot” user agents pretty aggressively.
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://finviz.com/",
    }
    r = session.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text


def parse_finviz_fundamentals(symbol: str, html: str) -> FinvizFundamentals:
    soup = BeautifulSoup(html, "html.parser")
    out = FinvizFundamentals(symbol=symbol)

    # Company name (best-effort)
    # Finviz often has it near the top in a <td class="fullview-title"> or similar.
    title_td = soup.find("td", class_=re.compile(r"fullview-title", re.I))
    if title_td:
        out.company = title_td.get_text(" ", strip=True)

    # Sector / Industry / Country links: usually three consecutive <a> near "fullview-links"
    links_td = soup.find("td", class_=re.compile(r"fullview-links", re.I))
    if links_td:
        a = links_td.find_all("a")
        if len(a) >= 3:
            out.sector = a[0].get_text(strip=True)
            out.industry = a[1].get_text(strip=True)
            out.country = a[2].get_text(strip=True)

    # Snapshot table: labels+values in alternating <td>
    snap = soup.find("table", class_=re.compile(r"snapshot-table2", re.I))
    kv: Dict[str, str] = {}
    if snap:
        tds = [td.get_text(" ", strip=True) for td in snap.find_all("td")]
        # alternating label/value
        for i in range(0, len(tds) - 1, 2):
            k = tds[i]
            v = tds[i + 1]
            kv[k] = v

    # Map fields
    # Common keys: 'Market Cap', 'P/E', 'Float', 'Float Short', 'Insider Trans', 'ATR'
    out.market_cap = parse_suffix_number(kv.get("Market Cap"))
    out.pe_ratio = parse_decimal(kv.get("P/E"))
    out.shares_float = parse_float_shares(kv.get("Float"))
    out.float_percent = parse_percent(kv.get("Float %"))  # sometimes exists, sometimes not
    out.short_float = parse_percent(kv.get("Float Short"))
    out.insider_transactions = parse_percent(kv.get("Insider Trans"))
    out.average_true_range = parse_decimal(kv.get("ATR"))

    # Exchange: Finviz doesn’t always expose cleanly in the snapshot.
    # Sometimes it’s embedded elsewhere; best-effort:
    if "Exchange" in kv:
        out.exchange = kv.get("Exchange")

    return out


# -----------------------------
# DB upserts
# -----------------------------

UPSERT_SYMBOL_METADATA_SQL = """
INSERT INTO symbol_metadata (symbol, db_tier, rt_tier, list_date, filters, last_updated)
VALUES (%s, %s, %s, %s, %s, NOW())
ON CONFLICT (symbol) DO UPDATE
SET db_tier = EXCLUDED.db_tier,
    rt_tier = EXCLUDED.rt_tier,
    list_date = COALESCE(EXCLUDED.list_date, symbol_metadata.list_date),
    filters = COALESCE(EXCLUDED.filters, symbol_metadata.filters),
    last_updated = NOW();
"""

UPSERT_FUNDAMENTALS_SQL = """
INSERT INTO fundamental_data
  (symbol, company, sector, industry, country, exchange, market_cap, pe_ratio,
   shares_float, float_percent, insider_transactions, short_float, average_true_range, last_updated)
VALUES
  (%s,%s,%s,%s,%s,%s,%s,%s,
   %s,%s,%s,%s,%s,NOW())
ON CONFLICT (symbol) DO UPDATE
SET company = EXCLUDED.company,
    sector = EXCLUDED.sector,
    industry = EXCLUDED.industry,
    country = EXCLUDED.country,
    exchange = EXCLUDED.exchange,
    market_cap = EXCLUDED.market_cap,
    pe_ratio = EXCLUDED.pe_ratio,
    shares_float = EXCLUDED.shares_float,
    float_percent = EXCLUDED.float_percent,
    insider_transactions = EXCLUDED.insider_transactions,
    short_float = EXCLUDED.short_float,
    average_true_range = EXCLUDED.average_true_range,
    last_updated = NOW();
"""


def upsert_symbol_metadata(
    conn: psycopg.Connection,
    symbol: str,
    db_tier: int,
    rt_tier: int,
    list_date: Optional[date],
    filters: Optional[list[str]],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            UPSERT_SYMBOL_METADATA_SQL,
            (symbol, db_tier, rt_tier, list_date, filters),
        )


def upsert_fundamentals(conn: psycopg.Connection, f: FinvizFundamentals) -> None:
    with conn.cursor() as cur:
        cur.execute(
            UPSERT_FUNDAMENTALS_SQL,
            (
                f.symbol,
                f.company,
                f.sector,
                f.industry,
                f.country,
                f.exchange,
                f.market_cap,
                f.pe_ratio,
                f.shares_float,
                f.float_percent,
                f.insider_transactions,
                f.short_float,
                f.average_true_range,
            ),
        )


# -----------------------------
# CLI
# -----------------------------

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Add symbols + fetch Finviz fundamentals into Postgres.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--symbol", help="Single symbol (e.g. KROS)")
    g.add_argument("--symbols-file", help="Text file with one symbol per line")

    p.add_argument("--db-tier", type=int, default=0, help="0=cold,1=watch,2=warm,3=hot (default 0)")
    p.add_argument("--rt-tier", type=int, default=0, help="0=cold,1=watch,2=warm,3=hot (default 0)")
    p.add_argument("--list-date", help="Optional list date YYYY-MM-DD")
    p.add_argument("--filter", action="append", default=[], help="Optional filter tag(s); repeatable")

    p.add_argument("--no-fetch", action="store_true", help="Only upsert symbol_metadata; skip Finviz fetch")
    p.add_argument("--sleep", type=float, default=0.8, help="Seconds to sleep between Finviz requests")
    p.add_argument("--batch", type=int, default=25, help="Commit every N symbols")

    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    if args.symbol:
        symbols = [norm_symbol(args.symbol)]
    else:
        with open(args.symbols_file, "r", encoding="utf-8") as f:
            symbols = [norm_symbol(line) for line in f if line.strip()]

    ld = date.fromisoformat(args.list_date) if args.list_date else None
    filters = args.filter if args.filter else None

    dsn = os.getenv("REFLEX_PG_DSN") or os.getenv("REFLEX__PG_DSN")
    if not dsn:
        raise RuntimeError("Required env REFLEX_PG_DSN (or REFLEX__PG_DSN) is not set")

    session = requests.Session()

    with psycopg.connect(dsn) as conn:
        conn.autocommit = False

        done = 0
        for group in chunked(symbols, args.batch):
            for sym in group:
                upsert_symbol_metadata(conn, sym, args.db_tier, args.rt_tier, ld, filters)

                if not args.no_fetch:
                    try:
                        html = fetch_finviz_html(sym, session)
                        f = parse_finviz_fundamentals(sym, html)
                        upsert_fundamentals(conn, f)
                        print(f"[OK] {sym} fundamentals updated: sector={f.sector} float={f.shares_float} mcap={f.market_cap}")
                    except Exception as e:
                        # Don’t crash the whole run because one ticker is weird.
                        print(f"[WARN] {sym} fetch/parse failed: {e}")

                    time.sleep(max(args.sleep, 0.0))

                done += 1

            conn.commit()
            print(f"[COMMIT] {done}/{len(symbols)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
