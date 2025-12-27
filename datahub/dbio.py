from __future__ import annotations
import os
from typing import Dict, Iterable, List
import psycopg
DB_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/stock_data")

def connect():
    return psycopg.connect(DB_URL)

def get_all_symbols() -> List[str]:
    sql = "SELECT DISTINCT symbol FROM symbol_metadata ORDER BY symbol"
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            return [r[0] for r in cur.fetchall()]
    except Exception:
        return []

def get_symbol_flags() -> Dict[str, Dict[str, bool]]:
    sql = "SELECT symbol, COALESCE(no_trade,false), COALESCE(halted,false), COALESCE(restricted,false) FROM symbol_flags"
    out: Dict[str, Dict[str, bool]] = {}
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            for sym, nt, h, r in cur.fetchall():
                out[sym.upper()] = {"no_trade": nt, "halted": h, "restricted": r}
    except Exception:
        pass
    return out

def get_initial_states() -> Dict[str, str]:
    sql = "SELECT symbol, state FROM symbol_states"
    out: Dict[str, str] = {}
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            for sym, st in cur.fetchall():
                out[sym.upper()] = (st or 'COLD').upper()
    except Exception:
        pass
    return out

def insert_trades(rows: Iterable[dict]) -> int:
    sql = '''
    INSERT INTO tick_data (symbol, timestamp, sip_timestamp, price, size, exchange, conditions, tape, participant_id)
    VALUES (%(symbol)s, %(ts_utc)s, %(sip_timestamp)s, %(price)s, %(size)s, %(exchange)s, %(conditions)s, %(tape)s, %(participant_id)s)
    ON CONFLICT DO NOTHING;
    '''
    n = 0
    with connect() as conn, conn.cursor() as cur:
        for r in rows:
            cur.execute(sql, r); n += 1
        conn.commit()
    return n
