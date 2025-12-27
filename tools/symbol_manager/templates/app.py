# =============================================================================
# app.py  — Reflex DB Manager (Flask)
# Version: 2025.10.22-rc2
#
# CHANGELOG
# - 2025-10-22: rc2
#   • Fixed Polygon backfill controller call signature to (symbol, mode).
#   • Updated /backfill_polygon_form route to pass symbol + mode only.
#   • Removed obsolete 'target' param; honors mode='all' and symbol=None/'ALL'.
# - 2025-10-22: rc1
#   • Registered ticks backfill UI (SSE popup logs).
#   • Centralized logging and DSN handling; added /health.
# =============================================================================
from __future__ import annotations
import os
import sys
import logging
from typing import Optional

from flask import Flask, render_template, request, redirect, url_for, flash, abort, jsonify

# --- Logging -----------------------------------------------------------------
log = logging.getLogger("DBManager")
log.setLevel(logging.INFO)
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
log.addHandler(_ch)

# ---- BOOTSTRAP --------------------------------------------------------------
_THIS_DIR = os.path.dirname(__file__)
_PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# --- Silence noisy FutureWarning from finvizfinance ---------------------------
import warnings
warnings.filterwarnings(
    "ignore",
    category=FutureWarning,
    module=r"finvizfinance\.screener\.base"
)


import psycopg

# existing modules in your repo
from finviz_fetch import fetch_finviz, hydrate_fundamental_metadata, hydrate_symbol_metadata
from db_init import drop_and_create_database
from db_backfill import run_polygon_backfill  # expects (symbol, mode)

# NEW: ticks backfill UI
from ticks_ui import bp as ticks_bp

# --- Flask app ---------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "872df3b4-1f4c-4e2b-8c3a-9e6f3b4e2b8c")
APP_PORT = int(os.getenv("APP_PORT", "5001"))
DSN = os.getenv("DATABASE_URL", "")

app.register_blueprint(ticks_bp)

def connection() -> psycopg.Connection:
    if not DSN:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(DSN)
import inspect

def _call_hydrator(func, *maybe_conn_df):
    """
    Robustly call a hydrator that may be defined as:
      - f(df)
      - f(conn, df)
    We inspect the signature at runtime and adapt.
    """
    try:
        sig = inspect.signature(func)
        params = list(sig.parameters.values())
        # Count only required positional-or-keyword params
        req = [p for p in params if p.default is inspect._empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        if len(req) == 1:
            # signature is (df) — pass only DataFrame
            return func(maybe_conn_df[-1])
        else:
            # assume (conn, df)
            return func(*maybe_conn_df)
    except Exception:
        # Fallback to old style (conn, df)
        try:
            return func(*maybe_conn_df)
        except TypeError:
            # Last-ditch: new style (df)
            return func(maybe_conn_df[-1])

# ------------------------
# Health
# ------------------------
@app.route("/health")
def health():
    return jsonify({"ok": True})

# ------------------------
# Home / Index
# ------------------------
@app.route("/")
def home():
    try:
        with connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM minute_bars")
            minute_rows = cur.fetchone()[0]
    except Exception:
        minute_rows = None
    return render_template("home.html", minute_rows=minute_rows)

@app.route("/index")
def index():
    return redirect(url_for("home"))

# ------------------------
# Initialize DB
# ------------------------
@app.route("/init_db_form", methods=["GET", "POST"])
def init_db_form():
    if request.method == "POST":
        drop_and_create_database()
        flash("Database schema initialized.", "success")
        log.info("Database schema initialized.")
        return redirect(url_for("home"))
    return render_template("init_db_form.html")

# ------------------------
# Finviz / Fundamentals
# ------------------------
@app.route("/finviz_prime_form", methods=["GET", "POST"])
def finviz_prime_form():
    if request.method == "POST":
        exchanges = request.form.getlist("exchanges") or ["NASDAQ", "NYSE", "AMEX"]
        market_cap = (request.form.get("market_cap") or "Small ($300mln to $2bln)").strip()
        price = (request.form.get("price") or "Over $1").strip()
        join_how = (request.form.get("join_how") or "outer").strip()
        sleep_between_calls_s = float(request.form.get("sleep_between_calls_s") or "0.25")

        valid_join_hows = ['outer', 'inner', 'left', 'right', 'cross', 'left_anti', 'right_anti']
        if join_how not in valid_join_hows:
            join_how = 'outer'

        extra_filters = {}
        extra_text = (request.form.get("extra_filters") or "").strip()
        if extra_text:
            for line in extra_text.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    k = k.strip(); v = v.strip()
                    if k and v:
                        extra_filters[k] = v

        df = fetch_finviz(
            exchanges=exchanges,
            market_cap=market_cap,
            price=price,
            join_how=join_how,
            extra_filters=extra_filters,
            sleep_between_calls_s=sleep_between_calls_s
        )

        # Hydrate using adaptive call (handles both (df) and (conn, df))
        try:
            with connection() as conn:
                _call_hydrator(hydrate_fundamental_metadata, conn, df)
                _call_hydrator(hydrate_symbol_metadata, conn, df)
        except TypeError:
            # If your hydrators manage their own connections, just call with df
            _call_hydrator(hydrate_fundamental_metadata, None, df)
            _call_hydrator(hydrate_symbol_metadata, None, df)

        flash(f"Imported fundamentals for {len(df)} symbols.", "success")
        return render_template("finviz_prime_form.html")
    return render_template("finviz_prime_form.html")


# ------------------------
# Backfill (Polygon) — FIXED to (symbol, mode)
# ------------------------
@app.route("/backfill_polygon_form", methods=["GET", "POST"])
def backfill_polygon_form():
    """
    Backfill controller.

    Behavior:
      - If 'symbol' is None/empty/'ALL'  -> run the selected mode for ALL symbols.
      - If 'mode' is 'all'               -> same as above (ALL symbols).
      - Else                             -> run mode for the single symbol.

    Modes supported per symbol: 'recent', 'moderate', 'full'.
    Fundamentals are refreshed per symbol.
    """
    if request.method == "POST":
        symbol = (request.form.get("symbol") or "").strip().upper()
        mode = (request.form.get("mode") or "recent").strip().lower()

        # Normalize controller expectations:
        # - symbol: None if empty/'ALL'
        # - mode: one of {'recent','moderate','full','all'}
        if symbol in ("", "ALL"):
            symbol_arg: Optional[str] = None
        else:
            symbol_arg = symbol

        if mode not in ("recent", "moderate", "full", "all"):
            mode = "recent"

        try:
            # run_polygon_backfill must accept (symbol, mode)
            # symbol=None or mode='all' => ALL symbols run inside the controller
            run_polygon_backfill(symbol_arg, mode)
            if symbol_arg is None or mode == "all":
                flash(f"Started Polygon backfill for ALL symbols (mode={mode}).", "success")
            else:
                flash(f"Started Polygon backfill for {symbol_arg} (mode={mode}).", "success")
        except Exception as e:
            flash(f"Backfill error: {e}", "error")
        return redirect(url_for("home"))

    return render_template("backfill_polygon_form.html")

# ------------------------
# Symbol Editor
# ------------------------
@app.route("/symbol_editor", methods=["GET"])
def symbol_editor():
    with connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT symbol, db_tier, filters FROM symbol_metadata ORDER BY symbol LIMIT 5000")
        rows = cur.fetchall()
    return render_template("symbol_editor.html", rows=rows)

@app.route("/symbol_editor_form", methods=["GET"])
def symbol_editor_form():
    return redirect(url_for("symbol_editor"))

# ---------------- API: list ----------------
@app.route("/api/symbols", methods=["GET"])
def api_list_symbols():
    q = """
        SELECT symbol,
               db_tier,
               CASE db_tier WHEN 0 THEN 'cold' WHEN 1 THEN 'watch' WHEN 2 THEN 'warm' ELSE 'hot' END AS db_label,
               COALESCE('do_not_trade' = ANY(filters), FALSE) AS do_not_trade
        FROM symbol_metadata
        ORDER BY symbol
        LIMIT 5000;
    """
    with connection() as conn, conn.cursor() as cur:
        cur.execute(q)
        rows = cur.fetchall()
    data = [{"symbol": r[0], "db_tier": int(r[1]), "db_label": r[2], "do_not_trade": bool(r[3])} for r in rows]
    return jsonify(data)

# ---------------- API: add/upsert ----------------
@app.route("/api/symbols", methods=["POST"])
def api_add_symbol():
    payload = request.get_json(force=True, silent=True) or {}
    sym = (payload.get("symbol") or "").strip().upper()
    if not sym:
        abort(400, "symbol required")

    sets = []
    params = []
    if "db_tier" in payload:
        sets.append("db_tier = %s"); params.append(int(payload["db_tier"]))
    if "rt_tier" in payload:
        sets.append("rt_tier = %s"); params.append(int(payload["rt_tier"]))

    add_dnt_sql = "UPDATE symbol_metadata SET filters = array_append(COALESCE(filters, ARRAY[]::text[]), 'do_not_trade') WHERE symbol = %s AND NOT ('do_not_trade' = ANY(filters))"
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
            SELECT symbol, db_tier, COALESCE('do_not_trade' = ANY(filters), FALSE) AS do_not_trade
            FROM symbol_metadata WHERE symbol = %s
            """,
            (sym,),
        )
        row = cur.fetchone()
        conn.commit()
    if not row:
        abort(404, "symbol not found")
    return jsonify({"symbol": row[0], "db_tier": int(row[1]), "do_not_trade": bool(row[2])})

# ------------------------
# Dashboard & Logs
# ------------------------
@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")

@app.route("/logs")
def logs():
    return render_template("logs.html")

# ------------------------
# Errors
# ------------------------
@app.errorhandler(404)
def not_found(e):
    try:
        return render_template("404.html"), 404
    except Exception:
        return "Not Found", 404

if __name__ == "__main__":
    app.run(debug=True, port=APP_PORT)
