#!/usr/bin/env python3
"""
finviz_prime_pump.py

Finviz prime pump:
- Screener -> symbol list (midcap 2B-10B, price>1 gate, stocks only)
- Quote PAGE (webpage) -> fundamental snapshot (float, market cap, sector, etc.)
- Writes:
    public.symbol_metadata  (filters tag)
    public.fundamental_data (light snapshot, no "price" storage)

Polygon fill tool later becomes authoritative and can overwrite/complete.

No L1 quotes. No ticks. No bars.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
import random
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    print("Error: requests module is required. Install with: pip install requests")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Error: beautifulsoup4 module is required. Install with: pip install beautifulsoup4")
    sys.exit(1)

FINVIZ_BASE = "https://finviz.com"
SCREENER_PATH = "/screener.ashx"
QUOTE_PATH = "/quote.ashx"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

# ----------------------------
# Filters
# ----------------------------

@dataclass(frozen=True)
class FinvizFilters:
    cap_over: str = "cap_midover"       # ~ >2B
    cap_under: str = "cap_midunder"     # ~ <10B
    price_over: str = "sh_price_o1"     # price > 1 (gate only)
    stocks_only: str = "ind_stocksonly" # stocks only

    def f_param(self) -> str:
        return ",".join([self.cap_over, self.cap_under, self.price_over, self.stocks_only])

def build_screener_url(filters: FinvizFilters, view: int = 111, start_row: int = 1) -> str:
    return f"{FINVIZ_BASE}{SCREENER_PATH}?v={view}&f={filters.f_param()}&r={start_row}"

# ----------------------------
# HTTP
# ----------------------------

def make_session(user_agent: str, cookie: str = "") -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": user_agent,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Connection": "keep-alive",
        "Referer": FINVIZ_BASE + "/",
    })
    if cookie:
        s.headers["Cookie"] = cookie
    return s

def polite_sleep(base_s: float, jitter_s: float = 0.4) -> None:
    if base_s <= 0:
        return
    time.sleep(base_s + random.random() * jitter_s)

def fetch_html(sess: requests.Session, url: str, timeout: int = 30, retries: int = 3) -> str:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = sess.get(url, timeout=timeout)
            r.raise_for_status()
            txt = r.text.lower()
            if "captcha" in txt or "access denied" in txt:
                raise RuntimeError("Finviz appears to be blocking requests (captcha/access denied).")
            return r.text
        except Exception as e:
            last_err = e
            time.sleep(0.7 * attempt)
    raise RuntimeError(f"Failed fetching {url}: {last_err}") from last_err

# ----------------------------
# Screener parse
# ----------------------------

def parse_screener_tickers(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    tickers: List[str] = []
    for a in soup.select("a[href*='quote.ashx?t=']"):
        href = a.get("href", "")
        m = re.search(r"quote\.ashx\?t=([A-Z0-9\.\-]+)", href)
        if m:
            sym = m.group(1).upper().strip()
            if 1 <= len(sym) <= 12:
                tickers.append(sym)
    seen = set()
    out: List[str] = []
    for t in tickers:
        if t not in seen:
            out.append(t)
            seen.add(t)
    return out

def fetch_all_tickers(sess: requests.Session, filters: FinvizFilters, sleep_s: float, max_pages: int) -> List[str]:
    all_syms: List[str] = []
    start_row = 1
    for _ in range(max_pages):
        url = build_screener_url(filters, view=111, start_row=start_row)
        print(f"[FINVIZ] fetching page start_row={start_row}")
        html = fetch_html(sess, url)
        page = parse_screener_tickers(html)
        if not page:
            break
        existing = set(all_syms)
        for s in page:
            print(f"  {s}") # log each symbol
            if s not in existing:
                all_syms.append(s)
                existing.add(s)
        start_row += 20
        polite_sleep(sleep_s)
    return all_syms

# ----------------------------
# Quote PAGE (web) parse -> fundamentals
# ----------------------------

IGNORE_KEYS = {
    # price-ish / performance-ish things we do NOT want to store from Finviz
    "Price","Change","Change %","Prev Close","Open","High","Low",
    "52W High","52W Low","Volume","Avg Volume","Rel Volume",
    "Perf Week","Perf Month","Perf Quarter","Perf Half Y","Perf Year","Perf YTD",
    "Volatility","Beta"
}

def parse_snapshot_table(html: str) -> Dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    data: Dict[str, str] = {}
    tds = soup.select("td.snapshot-td2")
    for i in range(0, len(tds) - 1, 2):
        k = tds[i].get_text(" ", strip=True)
        v = tds[i + 1].get_text(" ", strip=True)
        if k and v and k not in IGNORE_KEYS:
            data[k] = v
    return data

def fetch_finviz_fundamentals(sess: requests.Session, symbol: str) -> Dict[str, str]:
    url = f"{FINVIZ_BASE}{QUOTE_PATH}?t={symbol}"
    html = fetch_html(sess, url)
    return parse_snapshot_table(html)

def parse_abbrev_number(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.strip().replace(",", "")
    m = re.match(r"^(-?\d+(\.\d+)?)([KMBT])?$", s, re.IGNORECASE)
    if not m:
        return None
    val = float(m.group(1))
    suf = (m.group(3) or "").upper()
    mult = {"": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}.get(suf, 1)
    return val * mult

def parse_percent(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.strip().replace("%", "")
    try:
        return float(s)
    except Exception:
        return None

def parse_float_int(s: str) -> Optional[int]:
    n = parse_abbrev_number(s)
    if n is None:
        return None
    return int(round(n))

def map_to_fundamental_data(symbol: str, snap: Dict[str, str]) -> Dict[str, object]:
    out: Dict[str, object] = {"symbol": symbol}

    # Text
    if "Company" in snap:  out["company"] = snap["Company"]
    if "Sector" in snap:   out["sector"] = snap["Sector"]
    if "Industry" in snap: out["industry"] = snap["Industry"]
    if "Country" in snap:  out["country"] = snap["Country"]
    if "Exchange" in snap: out["exchange"] = snap["Exchange"]

    # Numeric
    if "Market Cap" in snap:
        mc = parse_abbrev_number(snap["Market Cap"])
        if mc is not None:
            out["market_cap"] = mc
    if "P/E" in snap:
        try:
            out["pe_ratio"] = float(snap["P/E"].replace(",", ""))
        except Exception:
            pass
    if "Float" in snap:
        fl = parse_float_int(snap["Float"])
        if fl is not None:
            out["shares_float"] = fl
    if "Float Short" in snap:
        p = parse_percent(snap["Float Short"])
        if p is not None:
            out["short_float"] = p
    if "Short Float" in snap:
        p = parse_percent(snap["Short Float"])
        if p is not None:
            out["short_float"] = p
    if "ATR" in snap:
        try:
            out["average_true_range"] = float(snap["ATR"].replace(",", ""))
        except Exception:
            pass
    if "Insider Trans" in snap:
        p = parse_percent(snap["Insider Trans"])
        if p is not None:
            out["insider_transactions"] = p
    if "Float %" in snap:
        p = parse_percent(snap["Float %"])
        if p is not None:
            out["float_percent"] = p

    return out

# ----------------------------
# Manifest
# ----------------------------

def write_manifest_csv(path: str, symbols: List[str], tag: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    gen = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "universe_tag", "generated_at_utc"])
        for s in symbols:
            w.writerow([s, tag, gen])

# ----------------------------
# DB (your schema)
# ----------------------------

def pg_connect():
    dsn = os.getenv("REFLEX__PG_DSN", "").strip()
    if dsn:
        return _pg_connect_dsn(dsn)
    return _pg_connect_dsn("")

def _pg_connect_dsn(dsn: str):
    try:
        import psycopg  # type: ignore
        return psycopg.connect(dsn)
    except Exception:
        pass
    try:
        import psycopg2  # type: ignore
        return psycopg2.connect(dsn)
    except Exception as e:
        raise RuntimeError("Install psycopg[binary] (preferred) or psycopg2.") from e

def ensure_tables(conn) -> None:
    cur = conn.cursor()
    cur.execute("""
      SELECT table_name
      FROM information_schema.tables
      WHERE table_schema='public'
        AND table_name IN ('symbol_metadata','fundamental_data');
    """)
    got = {r[0] for r in cur.fetchall()}
    cur.close()
    missing = {"symbol_metadata", "fundamental_data"} - got
    if missing:
        raise RuntimeError(f"Missing tables: {sorted(missing)} (apply schema.sql)")

def upsert_symbol_tag(conn, symbol: str, tag: str, set_list_date: bool) -> None:
    sql = """
    INSERT INTO public.symbol_metadata(symbol, filters, list_date, last_updated)
    VALUES (%s, ARRAY[%s]::text[], CASE WHEN %s THEN CURRENT_DATE ELSE NULL END, now())
    ON CONFLICT (symbol)
    DO UPDATE SET
      filters = CASE
        WHEN public.symbol_metadata.filters @> ARRAY[EXCLUDED.filters[1]]::text[] THEN public.symbol_metadata.filters
        ELSE public.symbol_metadata.filters || EXCLUDED.filters
      END,
      list_date = CASE
        WHEN %s AND public.symbol_metadata.list_date IS NULL THEN CURRENT_DATE
        ELSE public.symbol_metadata.list_date
      END,
      last_updated = now();
    """
    cur = conn.cursor()
    cur.execute(sql, (symbol, tag, set_list_date, set_list_date))
    conn.commit()
    cur.close()

def upsert_fundamental(conn, row: Dict[str, object]) -> None:
    sql = """
    INSERT INTO public.fundamental_data(
      symbol, company, sector, industry, country, exchange,
      market_cap, pe_ratio, shares_float, float_percent,
      insider_transactions, short_float, average_true_range, last_updated
    )
    VALUES (
      %(symbol)s, %(company)s, %(sector)s, %(industry)s, %(country)s, %(exchange)s,
      %(market_cap)s, %(pe_ratio)s, %(shares_float)s, %(float_percent)s,
      %(insider_transactions)s, %(short_float)s, %(average_true_range)s, now()
    )
    ON CONFLICT (symbol) DO UPDATE SET
      company = COALESCE(EXCLUDED.company, fundamental_data.company),
      sector = COALESCE(EXCLUDED.sector, fundamental_data.sector),
      industry = COALESCE(EXCLUDED.industry, fundamental_data.industry),
      country = COALESCE(EXCLUDED.country, fundamental_data.country),
      exchange = COALESCE(EXCLUDED.exchange, fundamental_data.exchange),
      market_cap = COALESCE(EXCLUDED.market_cap, fundamental_data.market_cap),
      pe_ratio = COALESCE(EXCLUDED.pe_ratio, fundamental_data.pe_ratio),
      shares_float = COALESCE(EXCLUDED.shares_float, fundamental_data.shares_float),
      float_percent = COALESCE(EXCLUDED.float_percent, fundamental_data.float_percent),
      insider_transactions = COALESCE(EXCLUDED.insider_transactions, fundamental_data.insider_transactions),
      short_float = COALESCE(EXCLUDED.short_float, fundamental_data.short_float),
      average_true_range = COALESCE(EXCLUDED.average_true_range, fundamental_data.average_true_range),
      last_updated = now();
    """
    payload = {k: row.get(k) for k in [
        "symbol","company","sector","industry","country","exchange",
        "market_cap","pe_ratio","shares_float","float_percent",
        "insider_transactions","short_float","average_true_range"
    ]}
    cur = conn.cursor()
    cur.execute(sql, payload)
    conn.commit()
    cur.close()

# ----------------------------
# CLI
# ----------------------------

def cmd_universe(args: argparse.Namespace) -> int:
    sess = make_session(args.user_agent, cookie=args.cookie)
    filters = FinvizFilters(args.cap_over, args.cap_under, args.price_over, args.stocks_only)

    print(f"[FINVIZ] screener f={filters.f_param()}")
    syms = fetch_all_tickers(sess, filters, sleep_s=args.sleep_s, max_pages=args.max_pages)
    if not syms:
        print("[ERROR] No symbols returned.")
        return 2

    print(f"[FINVIZ] {len(syms)} symbols")

    if args.out_csv:
        write_manifest_csv(args.out_csv, syms, args.tag)
        print(f"[OUT] {args.out_csv}")

    if args.pg:
        conn = pg_connect()
        ensure_tables(conn)

        for i, sym in enumerate(syms, 1):
            upsert_symbol_tag(conn, sym, args.tag, args.set_list_date)

            if args.capture_fundamentals:
                try:
                    snap = fetch_finviz_fundamentals(sess, sym)
                    row = map_to_fundamental_data(sym, snap)
                    upsert_fundamental(conn, row)
                except Exception as e:
                    print(f"[WARN] fundamentals failed {sym}: {e}")

                polite_sleep(args.sleep_s)

            if i % 100 == 0:
                print(f"[PROGRESS] {i}/{len(syms)}")

        try:
            conn.close()
        except Exception:
            pass
        print("[DB] done")

    return 0

def cmd_add_one(args: argparse.Namespace) -> int:
    sym = args.symbol.upper().strip()
    sess = make_session(args.user_agent, cookie=args.cookie)

    if not args.pg:
        print(sym)
        return 0

    conn = pg_connect()
    ensure_tables(conn)

    upsert_symbol_tag(conn, sym, args.tag, args.set_list_date)

    if args.capture_fundamentals:
        snap = fetch_finviz_fundamentals(sess, sym)
        row = map_to_fundamental_data(sym, snap)
        upsert_fundamental(conn, row)

    try:
        conn.close()
    except Exception:
        pass

    print(f"[DB] upserted {sym} tag={args.tag}")
    return 0

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Finviz prime pump: universe + fundamentals (no prices)")
    p.add_argument("--user-agent", default=DEFAULT_UA)
    p.add_argument("--cookie", default="")
    p.add_argument("--sleep-s", type=float, default=1.2)

    sub = p.add_subparsers(dest="cmd", required=True)

    u = sub.add_parser("universe")
    u.add_argument("--tag", default="universe_midcaps_v1")
    u.add_argument("--out-csv", default="")
    u.add_argument("--max-pages", type=int, default=300)
    u.add_argument("--pg", action="store_true")
    u.add_argument("--set-list-date", action="store_true")
    u.add_argument("--capture-fundamentals", action="store_true")

    u.add_argument("--cap-over", default="cap_midover")
    u.add_argument("--cap-under", default="cap_midunder")
    u.add_argument("--price-over", default="sh_price_o1")
    u.add_argument("--stocks-only", default="ind_stocksonly")

    a = sub.add_parser("add-one")
    a.add_argument("symbol")
    a.add_argument("--tag", default="manual_adds")
    a.add_argument("--pg", action="store_true")
    a.add_argument("--set-list-date", action="store_true")
    a.add_argument("--capture-fundamentals", action="store_true")

    return p

def main(argv: List[str]) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "universe":
        return cmd_universe(args)
    if args.cmd == "add-one":
        return cmd_add_one(args)
    return 2

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
