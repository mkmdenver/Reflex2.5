# dbmanager/app.py
from __future__ import annotations

import os
import sys
import logging
from datetime import datetime
from typing import Any, List

from flask import Flask, render_template, request, abort, redirect, url_for
from flask import jsonify

# -----------------------------------------------------------------------------
# Path bootstrap so running from repo root or dbmanager/ works
# -----------------------------------------------------------------------------
_THIS_DIR = os.path.dirname(__file__)                          # .../Reflex2/dbmanager
_PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, os.pardir))
_COMMON_DIR = os.path.join(_PROJECT_ROOT, "common")

for p in (_PROJECT_ROOT, _COMMON_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

# -----------------------------------------------------------------------------
# External deps / internal imports
# -----------------------------------------------------------------------------
from common.dbLayer.dbutils import connection  # context manager -> psycopg connection

# Finviz fetchers / hydrators
from dbmanager.finviz_fetch import (
    fetch_finviz,
    hydrate_fundamental_data,        # fundamentals upsert (kept name)
    hydrate_fundamental_metadata,    # compat alias to the same fundamentals routine
    hydrate_symbol_metadata,         # symbols upsert (db_tier/rt_tier)
)

# Backfill controller (recent/moderate/full + fundamentals)
from db_backfill import run_polygon_backfill

# -----------------------------------------------------------------------------
# App + logging
# -----------------------------------------------------------------------------
app = Flask(__name__, template_folder="templates", static_folder="static")
logging.basicConfig(
    level=os.environ.get("LOGLEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("dbmanager")

TIER_LABELS = {0: "cold", 1: "watch", 2: "warm", 3: "hot"}

# =============================================================================
# Health / Home
# =============================================================================
@app.route("/")
def home():
    return redirect(url_for("symbol_editor"))

@app.route("/health")
def health():
    stat = {"ok": True, "ts": datetime.utcnow().isoformat() + "Z"}
    # Try some quick counts, but never 500 here.
    try:
        with connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM symbol_metadata")
            sym_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM fundamental_data")
            fd_count = cur.fetchone()[0]
        stat.update({"symbols_total": int(sym_count), "fundamentals_total": int(fd_count)})
    except Exception as e:
        stat.update({"warn": str(e)})
    return jsonify(stat)

# =============================================================================
# Symbol Editor (page + JSON APIs) — NO BLUEPRINTS
# =============================================================================
@app.route("/symbol-editor", methods=["GET"])
def symbol_editor():
    # templates/symbol_editor.html must exist
    return render_template("symbol_editor.html")

# Back-compat alias (fixed to not recurse)
@app.route("/symbol_editor_form", methods=["GET"])
def symbol_editor_form():
    return redirect(url_for("symbol_editor"))

# ---- list --------------------------------------------------------------------
@app.route("/api/symbols", methods=["GET"])
def api_list_symbols():
    """
    Returns: [{symbol, db_tier, db_label, do_not_trade: bool}]
    Robust to: table missing, NULL db_tier
    """
    q = """
        SELECT
            symbol,
            COALESCE(db_tier, 0) AS db_tier,
            CASE COALESCE(db_tier, 0)
                WHEN 0 THEN 'cold' WHEN 1 THEN 'watch' WHEN 2 THEN 'warm' ELSE 'hot'
            END AS db_label,
            COALESCE('do_not_trade' = ANY(filters), FALSE) AS do_not_trade
        FROM symbol_metadata
        ORDER BY symbol
        LIMIT 5000;
    """
    try:
        with connection() as conn, conn.cursor() as cur:
            cur.execute(q)
            rows = cur.fetchall()
    except Exception as e:
        log.warning("/api/symbols: returning [] (table missing or startup): %s", e)
        return jsonify([])
    data = [
        {"symbol": r[0], "db_tier": int(r[1]), "db_label": r[2], "do_not_trade": bool(r[3])}
        for r in (rows or [])
    ]
    return jsonify(data)

# ---- add/upsert --------------------------------------------------------------
@app.route("/api/symbols", methods=["POST"])
def api_add_symbol():
    """
    Body: {symbol: str, db_tier: int (0..3), do_not_trade: bool?}
    Upserts into symbol_metadata (rt_tier untouched).
    """
    payload = request.get_json(silent=True) or {}
    symbol = str(payload.get("symbol", "")).strip().upper()
    if not symbol:
        abort(400, "symbol is required")

    try:
        db_tier = int(payload.get("db_tier", 0))
    except Exception:
        abort(400, "db_tier must be int 0..3")
    if not (0 <= db_tier <= 3):
        abort(400, "db_tier must be 0..3")

    want_dnt = bool(payload.get("do_not_trade", False))

    upsert_sql = """
        INSERT INTO symbol_metadata (symbol, db_tier, last_updated)
        VALUES (%s, %s, NOW())
        ON CONFLICT (symbol) DO UPDATE
           SET db_tier = EXCLUDED.db_tier,
               last_updated = NOW()
        RETURNING COALESCE('do_not_trade' = ANY(filters), FALSE) AS do_not_trade;
    """
    add_dnt_sql = """
        UPDATE symbol_metadata
           SET filters = (
               SELECT ARRAY(
                   SELECT DISTINCT x
                   FROM unnest(COALESCE(filters, ARRAY[]::text[]) || ARRAY['do_not_trade']) AS t(x)
               )
           )
         WHERE symbol = %s;
    """
    remove_dnt_sql = """
        UPDATE symbol_metadata
           SET filters = COALESCE(ARRAY(
               SELECT x FROM unnest(COALESCE(filters, ARRAY[]::text[])) AS t(x)
               WHERE x <> 'do_not_trade'
           ), ARRAY[]::text[])
         WHERE symbol = %s;
    """

    with connection() as conn, conn.cursor() as cur:
        cur.execute(upsert_sql, (symbol, db_tier))
        current_dnt = bool(cur.fetchone()[0])
        if want_dnt != current_dnt:
            cur.execute(add_dnt_sql if want_dnt else remove_dnt_sql, (symbol,))
        conn.commit()

    return jsonify({"ok": True})

# ---- update ------------------------------------------------------------------
@app.route("/api/symbols/<symbol>", methods=["PATCH", "POST"])
def api_update_symbol(symbol: str):
    """
    Body (any): {db_tier?: int 0..3, do_not_trade?: bool}
    """
    sym = symbol.strip().upper()
    payload = request.get_json(silent=True) or {}

    sets: List[str] = []
    params: List[Any] = []

    if "db_tier" in payload:
        try:
            db_tier = int(payload["db_tier"])
        except Exception:
            abort(400, "db_tier must be int 0..3")
        if not (0 <= db_tier <= 3):
            abort(400, "db_tier must be 0..3")
        sets.append("db_tier = %s")
        params.append(db_tier)

    if sets:
        sets.append("last_updated = NOW()")

    add_dnt_sql = """
        UPDATE symbol_metadata
           SET filters = (
               SELECT ARRAY(
                   SELECT DISTINCT x
                   FROM unnest(COALESCE(filters, ARRAY[]::text[]) || ARRAY['do_not_trade']) AS t(x)
               )
           )
         WHERE symbol = %s;
    """
    remove_dnt_sql = """
        UPDATE symbol_metadata
           SET filters = COALESCE(ARRAY(
               SELECT x FROM unnest(COALESCE(filters, ARRAY[]::text[])) AS t(x)
               WHERE x <> 'do_not_trade'
           ), ARRAY[]::text[])
         WHERE symbol = %s;
    """

    with connection() as conn, conn.cursor() as cur:
        if sets:
            cur.execute(f"UPDATE symbol_metadata SET {', '.join(sets)} WHERE symbol = %s", (*params, sym))

        if "do_not_trade" in payload:
            want = bool(payload["do_not_trade"])
            cur.execute(add_dnt_sql if want else remove_dnt_sql, (sym,))

        cur.execute(
            """
            SELECT symbol,
                   COALESCE(db_tier, 0) AS db_tier,
                   COALESCE('do_not_trade' = ANY(filters), FALSE) AS do_not_trade
            FROM symbol_metadata
            WHERE symbol = %s
            """,
            (sym,),
        )
        row = cur.fetchone()
        conn.commit()

    if not row:
        abort(404, "symbol not found")
    return jsonify({"symbol": row[0], "db_tier": int(row[1]), "do_not_trade": bool(row[2])})

# ---- delete ------------------------------------------------------------------
@app.route("/api/symbols/<symbol>", methods=["DELETE"])
def api_delete_symbol(symbol: str):
    sym = symbol.strip().upper()
    with connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM symbol_metadata WHERE symbol = %s", (sym,))
        conn.commit()
    return jsonify({"ok": True})

# =============================================================================
# Finviz "Prime" (fetch + upsert fundamentals + upsert symbols)
# =============================================================================
@app.route("/finviz/prime", methods=["GET"])
def finviz_prime_form():
    # Simple form; if you already have a template, render that instead.
    return render_template("finviz_prime_form.html") if os.path.exists(
        os.path.join(_THIS_DIR, "templates", "finviz_prime_form.html")
    ) else (
        "<h1>Finviz Prime</h1>"
        "<form method='post'>"
        " Exchanges: <label><input type='checkbox' name='exch' value='NASDAQ' checked>NASDAQ</label>"
        " <label><input type='checkbox' name='exch' value='NYSE' checked>NYSE</label>"
        " <label><input type='checkbox' name='exch' value='AMEX' checked>AMEX</label>"
        " <button type='submit'>Run</button>"
        "</form>"
    )

@app.route("/finviz/prime", methods=["POST"])
def finviz_prime_run():
    # exchanges from form or JSON
    if request.is_json:
        exchanges = request.json.get("exchanges") or ["NASDAQ", "NYSE", "AMEX"]
    else:
        exchanges = request.form.getlist("exch") or ["NASDAQ", "NYSE", "AMEX"]

    df = fetch_finviz(exchanges=exchanges)
    if df is None or df.empty:
        return jsonify({"ok": False, "message": "No rows from Finviz"}), 400

    n_fd = hydrate_fundamental_data(df)  # fundamentals
    n_sy = hydrate_symbol_metadata(df, default_db_tier=0, default_rt_tier=0)  # symbols

    return jsonify({"ok": True, "exchanges": exchanges, "fundamentals_upserts": n_fd, "symbols_upserts": n_sy})

# =============================================================================
# Backfill controller
# =============================================================================
@app.route("/backfill", methods=["POST"])
def backfill():
    """
    Body (JSON or form):
      symbol: 'ALL' | <ticker>
      mode:   'recent' | 'moderate' | 'full' | 'all' (all = ALL symbols; per-symbol mode via env)
    """
    if request.is_json:
        payload = request.get_json(silent=True) or {}
    else:
        payload = request.form or {}

    symbol = (payload.get("symbol") or "").strip().upper() or "ALL"
    mode = (payload.get("mode") or "recent").strip().lower()

    try:
        run_polygon_backfill(symbol if symbol != "ALL" else None, mode=mode)
        return jsonify({"ok": True, "symbol": symbol, "mode": mode})
    except Exception as e:
        log.exception("Backfill failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

# =============================================================================
# Error handlers (keep JSON bodies for XHR)
# =============================================================================
@app.errorhandler(400)
def _bad_request(e):
    return jsonify({"ok": False, "error": str(e)}), 400

@app.errorhandler(404)
def _not_found(e):
    return jsonify({"ok": False, "error": "not found"}), 404

@app.errorhandler(500)
def _server_error(e):
    return jsonify({"ok": False, "error": "internal error"}), 500

# =============================================================================
# Entrypoint
# =============================================================================
if __name__ == "__main__":
    # Example: set host/port with ENV if you like
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5001"))
    app.run(host=host, port=port, debug=bool(os.environ.get("DEBUG", "1") == "1"))
