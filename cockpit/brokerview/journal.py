import psycopg
from datetime import datetime, timezone
from config import settings

SCHEMA_SQL = '''
CREATE TABLE IF NOT EXISTS order_journal (
  id BIGSERIAL PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  account_id text NOT NULL,
  symbol text NOT NULL,
  side text NOT NULL,
  type text NOT NULL,
  qty numeric NOT NULL,
  limit_price numeric,
  stop_price numeric,
  tif text,
  extended_hours boolean,
  client_order_id text,
  status text NOT NULL,
  note text,
  raw jsonb
);
CREATE TABLE IF NOT EXISTS fill_journal (
  id BIGSERIAL PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  account_id text NOT NULL,
  order_id text NOT NULL,
  symbol text NOT NULL,
  qty numeric NOT NULL,
  price numeric NOT NULL,
  raw jsonb
);
'''

def _conn():
    return psycopg.connect(settings.PG_DSN, autocommit=True)

def ensure_schema():
    with _conn() as c:
        c.execute(SCHEMA_SQL)

def log_order(payload: dict):
    with _conn() as c:
        c.execute(
            '''
            INSERT INTO order_journal
            (ts, account_id, symbol, side, type, qty, limit_price, stop_price, tif, extended_hours,
             client_order_id, status, note, raw)
            VALUES (%(ts)s, %(account_id)s, %(symbol)s, %(side)s, %(type)s, %(qty)s, %(limit_price)s,
                    %(stop_price)s, %(tif)s, %(extended_hours)s, %(client_order_id)s, %(status)s, %(note)s, %(raw)s)
            ''',
            {"ts": datetime.now(timezone.utc), **payload},
        )
