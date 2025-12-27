
import os, psycopg2
from psycopg2.extras import RealDictCursor

def get_conn():
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(dsn)

def q(sql, params=None, fetch='all'):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params or ())
            if fetch == 'one':
                return cur.fetchone()
            if fetch == 'all':
                return cur.fetchall()
            return None

def exec_sql(sql_text: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_text)
