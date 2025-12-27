#!/usr/bin/env python
"""Preflight validator for the Bedrock schema.
Reads DATABASE_URL, connects with psycopg (v3), and checks existence of core
tables, views, triggers, Timescale extension, and continuous aggregates.
"""
import os, sys, psycopg
from psycopg.rows import tuple_row

REQUIRED_TABLES = [
    'tick_data','quote_data','minute_bars','daily_bars',
    'trade_triggers','minute_bar_audit','fundamental_data',
    'symbol_metadata','ingest_sessions','evaluator_flags','service_metrics'
]
REQUIRED_VIEWS = ['ticks','quotes','bars_1m']
REQUIRED_CAGGS = ['agg_5m_bars','agg_15m_bars','agg_1h_bars','agg_1d_bars','kpi_ingest_1m']
REQUIRED_TRIGGERS = [
    ('ticks','trg_ticks_insert'),
    ('quotes','trg_quotes_insert'),
    ('bars_1m','trg_bars1m_insert'),
]

def fail(msg):
    print(f"❌ {msg}")
    sys.exit(1)

def ok(msg):
    print(f"✅ {msg}")

def main():
    dsn = os.environ.get('DATABASE_URL')
    if not dsn:
        fail("DATABASE_URL not set.")
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            with conn.cursor(row_factory=tuple_row) as cur:
                # Timescale present
                cur.execute("SELECT extname FROM pg_extension WHERE extname='timescaledb';")
                if not cur.fetchone():
                    fail("timescaledb extension is not enabled in this database.")
                ok("timescaledb extension present.")

                # Tables
                cur.execute("""
                    SELECT relname FROM pg_class c
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='public' AND c.relkind='r';
                """)
                have_tables = {r[0] for r in cur.fetchall()}
                missing_tables = [t for t in REQUIRED_TABLES if t not in have_tables]
                if missing_tables:
                    fail("Missing tables: " + ", ".join(missing_tables))
                ok("All required tables present.")

                # Views and materialized views
                cur.execute("""
                    SELECT relname, relkind FROM pg_class c
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='public' AND c.relkind IN ('v','m');
                """)
                have_rels = {r[0]: r[1] for r in cur.fetchall()}
                have_views = set(have_rels.keys())
                missing_views = [v for v in REQUIRED_VIEWS if v not in have_views]
                if missing_views:
                    fail("Missing views: " + ", ".join(missing_views))
                ok("All required compatibility views present.")
                missing_caggs = [v for v in REQUIRED_CAGGS if v not in have_views]
                if missing_caggs:
                    fail("Missing continuous aggregates: " + ", ".join(missing_caggs))
                ok("All required continuous aggregates present.")

                # Triggers on views (INSTEAD OF)
                problems = []
                for view, trg in REQUIRED_TRIGGERS:
                    cur.execute("""
                        SELECT tgname FROM pg_trigger t
                        JOIN pg_class c ON c.oid = t.tgrelid
                        JOIN pg_namespace n ON n.oid=c.relnamespace
                        WHERE n.nspname='public' AND c.relname=%s AND NOT t.tgisinternal;
                    """, (view,))
                    have_trigs = {r[0] for r in cur.fetchall()}
                    if trg not in have_trigs:
                        problems.append(f"{view}.{trg}")
                if problems:
                    fail("Missing triggers: " + ", ".join(problems))
                ok("All required view insert-redirect triggers present.")

                ok("Bedrock schema validated successfully.")

    except Exception as e:
        fail(f"DB error: {e}")

if __name__ == '__main__':
    main()
