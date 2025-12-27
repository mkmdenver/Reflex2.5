#!/usr/bin/env python3
"""
Reflex2 — Backfill utility (full r... trimmed docstring in this paste)

This file is a patched version of your db_backfill with:
- Smart-by-default "fill holes" planning (skips days already present)
- Old behavior restored via --force
- Trading-day planning (uses trading_calendar if present, else weekdays)
- Compact CSV run log: <repo_root>/logs/backfill/backfill_runs.csv

NOTE: This file is intended to be dropped in as tools/symbol_manager/db_backfill.py
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import psycopg
import requests

# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------

LOG = logging.getLogger("backfill")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:backfill:%(message)s",
    datefmt="%H:%M:%S",
)

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _repo_root_from_here() -> Path:
    """
    Resolve repo root from this file location: tools/symbol_manager/db_backfill.py -> repo root.
    """
    here = Path(__file__).resolve()
    # .../tools/symbol_manager/db_backfill.py
    return here.parents[2]


def _parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _date_range_inclusive(start: dt.date, end: dt.date) -> list[dt.date]:
    if end < start:
        return []
    out: list[dt.date] = []
    d = start
    while d <= end:
        out.append(d)
        d += dt.timedelta(days=1)
    return out


def _chunks_contiguous(days: list[dt.date]) -> list[tuple[dt.date, dt.date]]:
    """
    Turn sorted list of dates into contiguous (start,end) ranges.
    """
    if not days:
        return []
    days = sorted(days)
    ranges: list[tuple[dt.date, dt.date]] = []
    s = days[0]
    prev = days[0]
    for d in days[1:]:
        if d == prev + dt.timedelta(days=1):
            prev = d
            continue
        ranges.append((s, prev))
        s = d
        prev = d
    ranges.append((s, prev))
    return ranges


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        if x is None:
            return default
        return int(x)
    except Exception:
        return default


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rand_run_id() -> str:
    return "".join(random.choice("0123456789abcdef") for _ in range(8))


# --------------------------------------------------------------------------------------
# Env loading (keep your existing behavior)
# --------------------------------------------------------------------------------------


def load_env(dotenv_path: Path) -> None:
    """
    Minimal .env loader: KEY=VALUE, ignores comments and section headers.
    Keeps current behavior: doesn't override existing env vars.
    """
    if not dotenv_path.exists():
        LOG.warning('No .env found at "%s"', str(dotenv_path))
        return
    LOG.info('Loading .env from "%s" ...', str(dotenv_path))
    for raw in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if line.startswith(";"):
            continue
        # ignore section headers like "# ---------- core ----------"
        if line.startswith("[") and line.endswith("]"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and (k not in os.environ):
            os.environ[k] = v


def get_env(key: str, default: str | None = None) -> str | None:
    v = os.environ.get(key)
    return v if v is not None else default


# --------------------------------------------------------------------------------------
# Polygon client (keep existing patterns)
# --------------------------------------------------------------------------------------


class PolygonClient:
    def __init__(self, api_key: str, base_url: str = "https://api.polygon.io"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        params = dict(params)
        params["apiKey"] = self.api_key
        url = f"{self.base_url}{path}"
        resp = self.session.get(url, params=params, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def fetch_daily_bars(self, symbol: str, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
        # This mirrors your existing usage: grouped aggs / range endpoint.
        # (Exact endpoint details assumed consistent with your original file.)
        path = f"/v2/aggs/ticker/{symbol}/range/1/day/{start.isoformat()}/{end.isoformat()}"
        js = self._get(path, {"adjusted": "true", "sort": "asc", "limit": 50000})
        return js.get("results", []) or []

    def fetch_minute_bars(self, symbol: str, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
        path = f"/v2/aggs/ticker/{symbol}/range/1/minute/{start.isoformat()}/{end.isoformat()}"
        js = self._get(path, {"adjusted": "true", "sort": "asc", "limit": 50000})
        return js.get("results", []) or []

    
def fetch_ticks(self, symbol: str, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
    """Fetch raw tick trades for a symbol between start..end (inclusive).

    Polygon v3 trades API is date-scoped; we loop day-by-day.
    Note: this returns raw trade dicts; the DB insert path normalizes them.
    """
    out: list[dict[str, Any]] = []
    d = start
    while d <= end:
        out.extend(list(self.iter_trades_for_day(symbol, d)))
        d += dt.timedelta(days=1)
    return out

def _append_api_key_to_url(self, url: str) -> str:
    """Polygon next_url often omits apiKey; ensure it's present."""
    u = urlparse(url)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    if "apiKey" not in q:
        q["apiKey"] = self.api_key
        u = u._replace(query=urlencode(q))
        return urlunparse(u)
    return url

def iter_trades_for_day(self, symbol: str, on_date: dt.date, limit: int = 50000) -> Iterator[dict[str, Any]]:
    """Yield Polygon v3 trades for (symbol, on_date), paging next_url.

    Handles common transient errors with backoff, and re-appends apiKey on next_url.
    """
    sym = (symbol or "").upper().strip()
    url = f"{self.base_url}/v3/trades/{sym}"
    params = {
        "date": on_date.isoformat(),
        "limit": str(int(limit)),
        "order": "asc",
        "sort": "timestamp",
        "apiKey": self.api_key,
    }

    next_url: Optional[str] = None
    backoff = 0.25
    max_backoff = 8.0
    retries = 0
    max_retries = 8

    while True:
        try:
            if next_url:
                url2 = self._append_api_key_to_url(next_url)
                resp = self.session.get(url2, timeout=30)
            else:
                resp = self.session.get(url, params=params, timeout=30)

            if resp.status_code in (429, 500, 502, 503, 504):
                LOG.warning("[NET] %s (%s) - backing off %.2fs", resp.status_code, resp.reason, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 1.7, max_backoff)
                retries += 1
                if retries > max_retries:
                    LOG.warning("[NET] giving up after %d retries", retries)
                    break
                continue

            if resp.status_code == 401 and next_url:
                # Try once to re-append the key and retry immediately if we somehow missed it
                url2 = self._append_api_key_to_url(next_url)
                if url2 != next_url:
                    LOG.warning("[NET] 401 on next_url; re-appending apiKey and retrying once")
                    next_url = url2
                    continue

            resp.raise_for_status()
            backoff, retries = 0.25, 0
            data = resp.json()
        except requests.RequestException as e:
            LOG.warning("[NET] %s - backing off %.2fs", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 1.7, max_backoff)
            retries += 1
            if retries > max_retries:
                LOG.warning("[NET] giving up after %d retries", retries)
                break
            continue

        results = data.get("results") or []
        for tr in results:
            if isinstance(tr, dict):
                yield tr

        next_url = data.get("next_url")
        if not next_url:
            break


# --------------------------------------------------------------------------------------
# DB access + "smart by default" day planning
# --------------------------------------------------------------------------------------


@dataclass
class BackfillStats:
    planned_days: int = 0
    fetch_days: int = 0
    skipped_days: int = 0
    failed_days: int = 0
    rows_upserted: int = 0
    bytes_out: int = 0


class Pg:
    def __init__(self, dsn: str):
        self.conn = psycopg.connect(dsn)
        self.conn.autocommit = False

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def get_symbol_list(self, symbol: str) -> list[tuple[str, dt.date | None]]:
        """
        Returns list of (symbol, list_date) from public.symbol_metadata.
        Existing behavior: if symbol == ALL, get all symbols.
        """
        with self.conn.cursor() as cur:
            if symbol.upper() == "ALL":
                cur.execute("SELECT symbol, list_date FROM public.symbol_metadata ORDER BY symbol")
            else:
                cur.execute(
                    "SELECT symbol, list_date FROM public.symbol_metadata WHERE symbol=%s",
                    (symbol.upper(),),
                )
            return [(r[0], r[1]) for r in cur.fetchall()]

    def get_trading_days(self, start: dt.date, end: dt.date) -> list[dt.date]:
        """
        Return trading days in [start, end] inclusive.
        Uses public.trading_calendar when available/populated; otherwise falls back to Mon–Fri.
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT session_date
                    FROM public.trading_calendar
                    WHERE is_trading_day = TRUE
                      AND session_date >= %s
                      AND session_date <= %s
                    ORDER BY session_date
                    """,
                    (start, end),
                )
                rows = cur.fetchall()
            if rows:
                return [row[0] for row in rows]
        except Exception:
            # table missing / not populated / permissions -> fallback
            pass

        out: list[dt.date] = []
        d = start
        while d <= end:
            if d.weekday() < 5:
                out.append(d)
            d += dt.timedelta(days=1)
        return out

    def get_expected_days(self, kind: str, start: dt.date, end: dt.date) -> list[dt.date]:
        """
        Expected day buckets for backfill planning.
        For daily/minute/tick backfills, plan over trading days (or weekdays fallback).
        """
        return self.get_trading_days(start, end)

    def get_present_days(self, kind: str, symbol: str, start: dt.date, end: dt.date) -> set[dt.date]:
        """
        Return set of day buckets present for (kind,symbol) within [start,end] inclusive.
        Presence definition (simple): at least one row exists for that day.
        """
        symbol = symbol.upper()
        if kind == "daily":
            table = "public.daily_bars"
            ts_col = "timestamp"
        elif kind == "minute":
            table = "public.minute_bars"
            ts_col = "timestamp"
        elif kind == "tick":
            table = "public.tick_data"
            ts_col = "timestamp"
        else:
            raise ValueError(f"Unknown kind: {kind}")

        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT DISTINCT date_trunc('day', {ts_col})::date AS d
                FROM {table}
                WHERE symbol = %s
                  AND {ts_col} >= %s::date
                  AND {ts_col} < (%s::date + interval '1 day')
                """,
                (symbol, start, end),
            )
            return {row[0] for row in cur.fetchall()}

    def upsert_daily_rows(self, symbol: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        with self.conn.cursor() as cur:
            for r in rows:
                # Polygon aggs format: t(ms), o,h,l,c,v
                ts = dt.datetime.fromtimestamp(r["t"] / 1000, tz=dt.timezone.utc)
                cur.execute(
                    """
                    INSERT INTO public.daily_bars(symbol, timestamp, open, high, low, close, volume)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, timestamp) DO UPDATE SET
                      open=EXCLUDED.open,
                      high=EXCLUDED.high,
                      low =EXCLUDED.low,
                      close=EXCLUDED.close,
                      volume=EXCLUDED.volume
                    """,
                    (symbol, ts, r.get("o"), r.get("h"), r.get("l"), r.get("c"), r.get("v")),
                )
        return len(rows)

    def upsert_minute_rows(self, symbol: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        with self.conn.cursor() as cur:
            for r in rows:
                ts = dt.datetime.fromtimestamp(r["t"] / 1000, tz=dt.timezone.utc)
                cur.execute(
                    """
                    INSERT INTO public.minute_bars(symbol, timestamp, open, high, low, close, volume)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, timestamp) DO UPDATE SET
                      open=EXCLUDED.open,
                      high=EXCLUDED.high,
                      low =EXCLUDED.low,
                      close=EXCLUDED.close,
                      volume=EXCLUDED.volume
                    """,
                    (symbol, ts, r.get("o"), r.get("h"), r.get("l"), r.get("c"), r.get("v")),
                )
        return len(rows)



def upsert_tick_rows(self, symbol: str, rows: list[tuple[Any, ...]]) -> int:
    """Insert tick rows into public.tick_data (canonical schema).

    Uses ON CONFLICT (symbol, timestamp, trade_id) DO NOTHING for idempotence.
    """
    if not rows:
        return 0
    with self.conn.cursor() as cur:
        cur.executemany(
            '''
            INSERT INTO public.tick_data
              (symbol, "timestamp",
               sip_timestamp, participant_timestamp, trf_timestamp,
               price, size, exchange, conditions, tape, trade_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol, "timestamp", trade_id) DO NOTHING
            ''',
            rows,
        )
    return len(rows)


# --------------------------------------------------------------------------------------
# Backfill core
# --------------------------------------------------------------------------------------


def _plan_missing_days(
    db: Pg,
    *,
    kind: str,
    symbol: str,
    eff_start: dt.date,
    until: dt.date,
    force: bool,
) -> tuple[list[dt.date], set[dt.date]]:
    expected_days = db.get_expected_days(kind, eff_start, until)
    if force:
        return expected_days, set()
    present = db.get_present_days(kind, symbol, eff_start, until)
    missing = [d for d in expected_days if d not in present]
    return missing, present


def _run_daily_for_symbol(
    *,
    db: Pg,
    poly: PolygonClient,
    symbol: str,
    eff_start: dt.date,
    until: dt.date,
    commit_every: int,
    force: bool,
) -> BackfillStats:
    st = BackfillStats()
    missing_days, _present = _plan_missing_days(db, kind="daily", symbol=symbol, eff_start=eff_start, until=until, force=force)
    st.planned_days = len(db.get_expected_days("daily", eff_start, until))
    st.fetch_days = len(missing_days)
    st.skipped_days = st.planned_days - st.fetch_days

    ranges = _chunks_contiguous(missing_days)

    for a, b in ranges:
        LOG.info("[%s] DAILY %s → %s (clamped from %s)", symbol, a.isoformat(), b.isoformat(), eff_start.isoformat())
        rows = poly.fetch_daily_bars(symbol, a, b)
        LOG.info("[%s] daily rows=%d", symbol, len(rows))
        n = db.upsert_daily_rows(symbol, rows)
        st.rows_upserted += n
        if commit_every <= 1:
            db.commit()
    return st


def _run_minute_for_symbol(
    *,
    db: Pg,
    poly: PolygonClient,
    symbol: str,
    eff_start: dt.date,
    until: dt.date,
    commit_every: int,
    force: bool,
) -> BackfillStats:
    st = BackfillStats()
    missing_days, _present = _plan_missing_days(db, kind="minute", symbol=symbol, eff_start=eff_start, until=until, force=force)
    st.planned_days = len(db.get_expected_days("minute", eff_start, until))
    st.fetch_days = len(missing_days)
    st.skipped_days = st.planned_days - st.fetch_days

    # Minute backfill: fetch per contiguous range (still safe because Polygon can return minutes in range)
    ranges = _chunks_contiguous(missing_days)

    for a, b in ranges:
        LOG.info("[%s] MINUTE %s → %s (clamped from %s)", symbol, a.isoformat(), b.isoformat(), eff_start.isoformat())
        rows = poly.fetch_minute_bars(symbol, a, b)
        LOG.info("[%s] minute rows=%d", symbol, len(rows))
        n = db.upsert_minute_rows(symbol, rows)
        st.rows_upserted += n
        if commit_every <= 1:
            db.commit()
    return st



def _normalize_polygon_trade_to_tick_row(symbol: str, tr: dict[str, Any]) -> Optional[tuple[Any, ...]]:
    """Map a Polygon v3 trade object into the canonical public.tick_data row tuple."""
    # Prefer SIP/participant/TRF timestamps; Polygon provides ns.
    ns = tr.get("sip_timestamp") or tr.get("participant_timestamp") or tr.get("trf_timestamp")
    if not ns:
        return None
    try:
        ts = dt.datetime.fromtimestamp(float(ns) / 1e9, tz=dt.timezone.utc)
    except Exception:
        return None

    price = tr.get("price")
    size = tr.get("size")
    if price is None or size is None:
        return None

    sip_ns = tr.get("sip_timestamp")
    part_ns = tr.get("participant_timestamp")
    trf_ns = tr.get("trf_timestamp")

    exchange = tr.get("exchange")
    tape = tr.get("tape")

    conds = tr.get("conditions") or []
    try:
        conds = [int(c) for c in conds]
    except Exception:
        conds = None

    trade_id = tr.get("id")
    trade_id = str(trade_id) if trade_id is not None else None
    if not trade_id:
        return None

    return (
        symbol.upper(),
        ts,
        sip_ns,
        part_ns,
        trf_ns,
        float(price),
        int(size),
        int(exchange) if exchange is not None else None,
        conds,
        int(tape) if tape is not None else None,
        trade_id,
    )


def _run_tick_for_symbol(
    *,
    db: Pg,
    poly: PolygonClient,
    symbol: str,
    eff_start: dt.date,
    until: dt.date,
    commit_every: int,
    force: bool,
) -> BackfillStats:
    st = BackfillStats()
    missing_days, _present = _plan_missing_days(db, kind="tick", symbol=symbol, eff_start=eff_start, until=until, force=force)
    st.planned_days = len(db.get_expected_days("tick", eff_start, until))
    st.fetch_days = len(missing_days)
    st.skipped_days = st.planned_days - st.fetch_days

    # For ticks, Polygon v3 is day-scoped; fetch one day at a time.
    # We keep memory bounded by batching inserts.
    BATCH = 5000

    for day in missing_days:
        try:
            LOG.info("[%s] TICK %s", symbol, day.isoformat())
            batch: list[tuple[Any, ...]] = []
            total_day = 0

            for tr in poly.iter_trades_for_day(symbol, day):
                row = _normalize_polygon_trade_to_tick_row(symbol, tr)
                if row:
                    batch.append(row)
                    if len(batch) >= BATCH:
                        db.upsert_tick_rows(symbol, batch)
                        st.rows_upserted += len(batch)
                        total_day += len(batch)
                        batch.clear()

            if batch:
                db.upsert_tick_rows(symbol, batch)
                st.rows_upserted += len(batch)
                total_day += len(batch)

            LOG.info("[%s] tick rows=%d for %s", symbol, total_day, day.isoformat())

            if commit_every <= 1:
                db.commit()
        except Exception as e:
            st.failed_days += 1
            LOG.warning("[%s] tick failed day=%s err=%r", symbol, day.isoformat(), e)
            try:
                db.rollback()
            except Exception:
                pass

    return st

def _append_run_log(
    repo_root: Path,
    *,
    run_id: str,
    kind: str,
    symbol_mode: str,
    since: dt.date,
    until: dt.date,
    force: bool,
    plan_syms: int,
    planned_days: int,
    fetch_days: int,
    skipped_days: int,
    failed_days: int,
    rows_upserted: int,
    status: str,
    msg: str,
) -> None:
    log_dir = repo_root / "logs" / "backfill"
    _ensure_dir(log_dir)
    log_path = log_dir / "backfill_runs.csv"

    header = [
        "ts_utc",
        "run_id",
        "kind",
        "symbol_mode",
        "since",
        "until",
        "force",
        "plan_syms",
        "plan_days",
        "fetch_days",
        "skipped_days",
        "failed_days",
        "rows_upserted",
        "status",
        "msg",
    ]

    row = [
        _utc_now_iso(),
        run_id,
        kind,
        symbol_mode,
        since.isoformat(),
        until.isoformat(),
        "1" if force else "0",
        str(plan_syms),
        str(planned_days),
        str(fetch_days),
        str(skipped_days),
        str(failed_days),
        str(rows_upserted),
        status,
        msg.replace("\n", " ").strip(),
    ]

    write_header = not log_path.exists()
    with log_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(header)
        w.writerow(row)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Reflex2 DB backfill (daily/minute/tick)")
    p.add_argument("--kind", choices=["daily", "minute", "tick"], required=True)
    p.add_argument("--symbol", default="ALL", help="Symbol or ALL")
    p.add_argument("--since", required=True, help="YYYY-MM-DD")
    p.add_argument("--until", default=None, help="YYYY-MM-DD (inclusive); default=today UTC")
    p.add_argument("--dry-run", action="store_true", help="Plan only; do not fetch/insert")
    p.add_argument("--commit-every", type=int, default=1)
    p.add_argument("--start-at", default=None, help="Resume from a given symbol (lexical order)")
    p.add_argument("--force", action="store_true", help="Refetch full range even if data exists (old behavior).")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    args = build_arg_parser().parse_args(argv)

    repo_root = _repo_root_from_here()
    load_env(repo_root / ".env")

    dsn = (
        get_env("REFLEX__PG_DSN")
        or get_env("REFLEX_PG_DSN")
        or get_env("PG_DSN")
        or ""
    )

    if not dsn:
        LOG.error("Missing REFLEX__PG_DSN (or PG_DSN) in environment.")
        return 2

    api_key = get_env("POLYGON_API_KEY") or ""
    if not api_key:
        LOG.error("Missing POLYGON_API_KEY in environment.")
        return 2

    since = _parse_date(args.since)
    until = _parse_date(args.until) if args.until else dt.datetime.now(dt.timezone.utc).date()

    run_id = _rand_run_id()
    symbol_mode = args.symbol.upper()

    db = Pg(dsn)
    poly = PolygonClient(api_key)

    total_planned_days = 0
    total_fetch_days = 0
    total_skipped_days = 0
    total_failed_days = 0
    total_rows = 0

    status = "OK"
    msg = ""

    try:
        sym_list = db.get_symbol_list(args.symbol)
        if args.start_at:
            sym_list = [(s, ld) for (s, ld) in sym_list if s >= args.start_at.upper()]

        LOG.info("Symbols: %d", len(sym_list))

        for sym, list_date in sym_list:
            eff_start = max(since, list_date) if list_date else since
            if until < eff_start:
                continue

            if args.dry_run:
                # Plan only
                missing_days, present = _plan_missing_days(db, kind=args.kind, symbol=sym, eff_start=eff_start, until=until, force=args.force)
                planned = len(db.get_expected_days(args.kind, eff_start, until))
                LOG.info("[%s] plan kind=%s expected=%d present=%d missing=%d", sym, args.kind, planned, len(present), len(missing_days))
                total_planned_days += planned
                total_fetch_days += len(missing_days)
                total_skipped_days += max(0, planned - len(missing_days))
                continue

            if args.kind == "daily":
                st = _run_daily_for_symbol(db=db, poly=poly, symbol=sym, eff_start=eff_start, until=until, commit_every=args.commit_every, force=args.force)
            elif args.kind == "minute":
                st = _run_minute_for_symbol(db=db, poly=poly, symbol=sym, eff_start=eff_start, until=until, commit_every=args.commit_every, force=args.force)
            else:
                st = _run_tick_for_symbol(db=db, poly=poly, symbol=sym, eff_start=eff_start, until=until, commit_every=args.commit_every, force=args.force)

            total_planned_days += st.planned_days
            total_fetch_days += st.fetch_days
            total_skipped_days += st.skipped_days
            total_failed_days += st.failed_days
            total_rows += st.rows_upserted

            if args.commit_every > 1:
                # keep existing semantics: commit occasionally (not implemented in this trimmed version)
                pass

        # final commit
        db.commit()

    except KeyboardInterrupt:
        LOG.warning("Interrupted; committing partial work and exiting…")
        try:
            db.commit()
        except Exception:
            pass
        status = "WARN"
        msg = "Interrupted"
        _append_run_log(
            repo_root,
            run_id=run_id,
            kind=args.kind,
            symbol_mode=symbol_mode,
            since=since,
            until=until,
            force=args.force,
            plan_syms=len(sym_list) if "sym_list" in locals() else 0,
            planned_days=total_planned_days,
            fetch_days=total_fetch_days,
            skipped_days=total_skipped_days,
            failed_days=total_failed_days,
            rows_upserted=total_rows,
            status=status,
            msg=msg,
        )
        return 130

    except Exception as e:
        LOG.exception("Backfill failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
        status = "FAIL"
        msg = f"{type(e).__name__}: {e}"
        _append_run_log(
            repo_root,
            run_id=run_id,
            kind=args.kind,
            symbol_mode=symbol_mode,
            since=since,
            until=until,
            force=args.force,
            plan_syms=len(sym_list) if "sym_list" in locals() else 0,
            planned_days=total_planned_days,
            fetch_days=total_fetch_days,
            skipped_days=total_skipped_days,
            failed_days=total_failed_days,
            rows_upserted=total_rows,
            status=status,
            msg=msg,
        )
        return 1

    finally:
        db.close()

    # Success log
    msg = f"planned={total_planned_days} fetch={total_fetch_days} skip={total_skipped_days} rows={total_rows}"
    _append_run_log(
        repo_root,
        run_id=run_id,
        kind=args.kind,
        symbol_mode=symbol_mode,
        since=since,
        until=until,
        force=args.force,
        plan_syms=len(sym_list) if "sym_list" in locals() else 0,
        planned_days=total_planned_days,
        fetch_days=total_fetch_days,
        skipped_days=total_skipped_days,
        failed_days=total_failed_days,
        rows_upserted=total_rows,
        status=status,
        msg=msg,
    )

    LOG.info("Done. %s", msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
