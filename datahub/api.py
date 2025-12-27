# ... existing imports ...
import psycopg
import os
from datetime import timezone

from flask import Flask, jsonify, request

from common.bus import CHANNELS, publish_sync

flask_app = Flask(__name__)

# --- NEW: DB config (reuse what backfill_worker uses) --------------------

PG_DSN = (
    os.getenv("REFLEX_PG_DSN")
    or os.getenv("REFLEX__PG_DSN")
    or os.getenv("DATABASE_URL")
)


def _fetch_minute_bars(
    symbol: str,
    limit: int,
    since_ts_ns: int | None = None,
) -> list[dict]:
    """
    Fetch recent minute bars from Postgres.

    Returns a list of dicts in internal FTS shape:
      { "ts": <ns_since_epoch>, "close": <float>, "volume": <int> }
    """
    if not PG_DSN:
        return []

    sym = symbol.upper()
    sql = """
        SELECT "timestamp", close, volume
        FROM minute_bars
        WHERE symbol = %s
    """

    params: list = [sym]

    if since_ts_ns is not None:
        # ns -> seconds for to_timestamp
        since_ts_sec = since_ts_ns / 1_000_000_000.0
        sql += ' AND "timestamp" > to_timestamp(%s)'
        params.append(since_ts_sec)

    sql += ' ORDER BY "timestamp" DESC LIMIT %s'
    params.append(limit)

    rows: list[dict] = []
    try:
        with psycopg.connect(PG_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                raw = cur.fetchall()
    except Exception:
        return []

    # We got newest-first; normalize to oldest-first
    for ts, close_val, vol_val in reversed(raw):
        try:
            ts_ns = int(ts.replace(tzinfo=timezone.utc).timestamp() * 1_000_000_000)
            close_f = float(close_val)
            vol_i = int(vol_val or 0)
        except Exception:
            continue
        rows.append(
            {
                "ts": ts_ns,
                "close": close_f,
                "volume": vol_i,
            }
        )
    return rows


# --- existing /health and /v1/tiers/service endpoints stay as-is ---


@flask_app.get("/v1/history/bars1m")
def history_bars_1m():
    """
    Return recent 1-minute bars for a symbol.

    Query params:
      - symbol   (required)
      - limit    (optional, default 240)
      - since_ts (optional, ns since epoch; if provided, only bars AFTER this)
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
