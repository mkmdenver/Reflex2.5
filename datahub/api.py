# datahub/api.py
import os
from datetime import timezone

import psycopg
from flask import Flask, jsonify, request

from common.bus import CHANNELS, publish_sync

flask_app = Flask(__name__)

# -----------------------------------------------------------------------------
# ENV POLICY (IMPORTANT)
# -----------------------------------------------------------------------------
# This process MUST be launched via a .bat that calls repo-root env.bat.
# We do NOT load .env/.env.local inside Python anymore.
# If REFLEX_PG_DSN (or fallback) is missing, we'll log and return [].
# -----------------------------------------------------------------------------

def _get_pg_dsn() -> str | None:
    """
    Runtime DSN getter (do NOT cache at import time).
    Env must already be loaded by env.bat.
    """
    return (
        os.getenv("REFLEX_PG_DSN")
        or os.getenv("REFLEX__PG_DSN")
        or os.getenv("DATABASE_URL")
    )


# --- robust history store config (env-overridable) ---------------------------
# Defaults assume the canonical table is public.minute_bars with columns:
#   symbol (text), "timestamp" (timestamptz), close (numeric/float), volume (int)
BARS1M_TABLE = os.getenv("DATAHUB_BARS1M_TABLE", "public.minute_bars")
BARS1M_COL_TS = os.getenv("DATAHUB_BARS1M_COL_TS", "timestamp")
BARS1M_COL_CLOSE = os.getenv("DATAHUB_BARS1M_COL_CLOSE", "close")
BARS1M_COL_VOL = os.getenv("DATAHUB_BARS1M_COL_VOL", "volume")


def _safe_ident(x: str) -> str:
    """
    Very small safety check for SQL identifiers. psycopg can't parameterize
    identifiers, so we ensure only safe characters are present.
    """
    s = (x or "").strip()
    if not s:
        raise ValueError("empty identifier")
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_."
    for ch in s:
        if ch not in allowed:
            raise ValueError(f"unsafe identifier: {s!r}")
    return s


def _fetch_minute_bars(symbol: str, limit: int, since_ts_ns: int | None = None) -> list[dict]:
    print(f"[history.bars1m] fetching {limit} bars for {symbol} since {since_ts_ns}")
    """
    Robust fetch of 1-minute bars.

    Preserves existing behavior:
      - returns [] on failure
      - returns bars oldest->newest as {ts, close, volume}
    But it will NOT fail silently anymore.
    """
    pg_dsn = _get_pg_dsn()
    if not pg_dsn:
        print("[history.bars1m] PG_DSN missing (launch via env.bat)")
        print("[history.bars1m] keys set:", {
            "REFLEX_PG_DSN": bool(os.getenv("REFLEX_PG_DSN")),
            "REFLEX__PG_DSN": bool(os.getenv("REFLEX__PG_DSN")),
            "DATABASE_URL": bool(os.getenv("DATABASE_URL")),
        })
        return []

    sym = (symbol or "").strip().upper()
    if not sym:
        return []

    try:
        limit = int(limit)
    except Exception:
        limit = 240
    limit = max(1, min(limit, 20000))

    # Prefer canonical table per your schema: public.minute_bars(timestamp, close, volume)
    # Fallback to view public.bars_1m(bar_time, close, volume) if needed.
    queries: list[tuple[str, list]] = []

    base_params: list = [sym]

    if since_ts_ns is not None:
        try:
            since_ts_sec = since_ts_ns / 1_000_000_000.0
        except Exception:
            since_ts_sec = None
    else:
        since_ts_sec = None

    # 1) Canonical table
    sql1 = """
        SELECT "timestamp", close, volume
        FROM public.minute_bars
        WHERE symbol = %s
    """
    params1 = base_params.copy()
    if since_ts_sec is not None:
        sql1 += ' AND "timestamp" > to_timestamp(%s)'
        params1.append(since_ts_sec)
    sql1 += ' ORDER BY "timestamp" DESC LIMIT %s'
    params1.append(limit)
    queries.append((sql1, params1))

    # 2) Fallback view (if someone renamed timestamp or table drifted)
    sql2 = """
        SELECT bar_time, close, volume
        FROM public.bars_1m
        WHERE symbol = %s
    """
    params2 = base_params.copy()
    if since_ts_sec is not None:
        sql2 += ' AND bar_time > to_timestamp(%s)'
        params2.append(since_ts_sec)
    sql2 += ' ORDER BY bar_time DESC LIMIT %s'
    params2.append(limit)
    queries.append((sql2, params2))

    last_err = None
    raw = None
    used = None

    try:
        with psycopg.connect(pg_dsn) as conn:
            with conn.cursor() as cur:
                for sql, params in queries:
                    try:
                        cur.execute(sql, params)
                        raw = cur.fetchall()
                        used = sql.strip().splitlines()[1].strip()  # "SELECT ..."
                        break
                    except Exception as exc:
                        last_err = exc
                        continue
    except Exception as exc:
        print(f"[history.bars1m] connect/query error: {exc!r}")
        return []

    if raw is None:
        # Both queries failed — tell the truth
        print(f"[history.bars1m] ALL QUERIES FAILED: {last_err!r}")
        return []

    # Normalize newest-first -> oldest-first
    rows: list[dict] = []
    for ts, close_val, vol_val in reversed(raw):
        try:
            ts_ns = int(ts.replace(tzinfo=timezone.utc).timestamp() * 1_000_000_000)
            close_f = float(close_val)
            vol_i = int(vol_val or 0)
        except Exception:
            continue
        rows.append({"ts": ts_ns, "close": close_f, "volume": vol_i})

    # Optional: one-line debug when empty (helps you separate “no data” from “query broke”)
    if not rows:
        print(f"[history.bars1m] EMPTY result for {sym} using {used}")

    return rows


@flask_app.get("/v1/history/bars1m")
def history_bars_1m():
    print("[history.bars1m] request received")
    """
    Return recent 1-minute bars for a symbol.

    Query params:
      - symbol   (required)
      - limit    (optional, default 240)
      - since_ts (optional, ns since epoch; if provided, only bars AFTER this)

    Response shape preserved:
      { ok, symbol, limit, since_ts, count, bars }
    """
    symbol = (request.args.get("symbol") or "").upper()
    if not symbol:
        return jsonify({"ok": False, "error": "symbol is required"}), 400

    try:
        limit = int(request.args.get("limit") or 240)
    except ValueError:
        limit = 240
    if limit <= 0:
        limit = 1

    since_raw = request.args.get("since_ts")
    since_ts_ns: int | None = None
    if since_raw:
        try:
            since_ts_ns = int(since_raw)
        except ValueError:
            since_ts_ns = None

    bars = _fetch_minute_bars(symbol, limit, since_ts_ns)

    return jsonify(
        {
            "ok": True,
            "symbol": symbol,
            "limit": limit,
            "since_ts": since_ts_ns,
            "count": len(bars),
            "bars": bars,
        }
    )
