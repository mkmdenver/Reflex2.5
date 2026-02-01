# trader/risk_sync.py
from __future__ import annotations
import asyncio
import os
import signal
import sys
import time
from contextlib import asynccontextmanager

import psycopg
from psycopg.rows import dict_row

from common.utils import load_env
from common import logging as log

COMPONENT = "trader.risk_sync"

# ---- force Selector loop on Windows (psycopg async needs it) ----------------
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception as e:
        # non-fatal; we’ll still try to run
        log.warn(COMPONENT, "win_selector_policy_failed", err=str(e))

# ---- config from env ---------------------------------------------------------
_env = load_env()

INSTANCE_ID = os.getenv("REFLEX__INSTANCE_ID", "liveA")
PG_DSN = (
    os.getenv("REFLEX__STORAGE__PG_DSN")
    or os.getenv("REFLEX__PG_DSN")
    or os.getenv("DATABASE_URL")
)

# runtime tunables
ORDERS_MAX_DEFAULT    = int(os.getenv("REFLEX__ORDERS_MAX_DEFAULT", "8"))
STREAM_MAXLEN_DEFAULT = int(os.getenv("REFLEX__STREAM_MAXLEN", "100000"))
POLL_SEC              = int(os.getenv("REFLEX__RISK_SYNC_POLL_SEC", "5"))
ALLOW_CREATE          = os.getenv("REFLEX__RISK_SYNC_ALLOW_CREATE", "true").lower() not in ("0","false","no")

_WARN_EVERY_SEC = 60
_last_warn = 0.0
_stop = asyncio.Event()


# ---- pg helpers --------------------------------------------------------------
@asynccontextmanager
async def pg_conn(dsn: str):
    if not dsn:
        raise RuntimeError(
            "No Postgres DSN set (REFLEX__STORAGE__PG_DSN / REFLEX__PG_DSN / DATABASE_URL)."
        )
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        yield conn


async def ensure_table(conn) -> None:
    if not ALLOW_CREATE:
        return
    ddl = """
    CREATE TABLE IF NOT EXISTS public.trader_risk (
      instance_id   text PRIMARY KEY,
      orders_max    integer NOT NULL,
      stream_maxlen integer NOT NULL,
      updated_at    timestamptz NOT NULL DEFAULT now()
    );
    """
    async with conn.cursor() as cur:
        await cur.execute(ddl)


async def seed_instance_row(conn, instance_id: str, orders_max: int, stream_maxlen: int) -> None:
    """Insert a row for this instance if it doesn't exist yet (one-time)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT 1 FROM public.trader_risk WHERE instance_id = %s",
            (instance_id,),
        )
        exists = await cur.fetchone()
        if not exists:
            await cur.execute(
                """
                INSERT INTO public.trader_risk (instance_id, orders_max, stream_maxlen)
                VALUES (%s, %s, %s)
                """,
                (instance_id, orders_max, stream_maxlen),
            )
            log.info(
                COMPONENT, "seeded_defaults",
                instance_id=instance_id, orders_max=orders_max, stream_maxlen=stream_maxlen
            )


async def read_risk(conn) -> tuple[int, int, str]:
    """Return (orders_max, stream_maxlen, src). Prefer DB; fall back to env."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT orders_max, stream_maxlen
            FROM public.trader_risk
            WHERE instance_id = %s
            """,
            (INSTANCE_ID,),
        )
        row = await cur.fetchone()
        if row:
            return int(row["orders_max"]), int(row["stream_maxlen"]), "db"
    return ORDERS_MAX_DEFAULT, STREAM_MAXLEN_DEFAULT, "env"


# ---- small logging nicety ----------------------------------------------------
def warn_rate_limited(msg_key: str, **fields):
    global _last_warn
    now = time.time()
    if now - _last_warn >= _WARN_EVERY_SEC:
        log.warn(COMPONENT, msg_key, **fields)
        _last_warn = now


# ---- main loop ---------------------------------------------------------------
async def sync_loop(dsn: str):
    log.info(COMPONENT, "connecting")
    async with pg_conn(dsn) as conn:
        log.info(COMPONENT, "connected")

        # 1) ensure table exists (optional)
        try:
            await ensure_table(conn)
        except Exception as e:
            log.warn(COMPONENT, "ensure_table_failed", err=str(e))

        # 2) seed one-time if missing
        try:
            await seed_instance_row(conn, INSTANCE_ID, ORDERS_MAX_DEFAULT, STREAM_MAXLEN_DEFAULT)
        except Exception as e:
            log.warn(COMPONENT, "seed_failed", err=str(e))

        # 3) poll for changes until stopped
        while not _stop.is_set():
            try:
                orders_max, stream_maxlen, src = await read_risk(conn)
                from trader.core import Store  # delayed import to avoid circular dependency
                s = Store()
                s.set_orders_capacity(orders_max)
                s.set_events_maxlen(stream_maxlen)

                log.info(
                    COMPONENT, "applied_runtime_limits",
                    orders_max=orders_max, events_maxlen=stream_maxlen, src=src
                )

                if src == "env":
                    warn_rate_limited(
                        "risk_row_missing_using_env",
                        instance_id=INSTANCE_ID,
                        orders_max=orders_max,
                        stream_maxlen=stream_maxlen
                    )
                log.info(
                    COMPONENT, "effective_risk",
                    instance_id=INSTANCE_ID, orders_max=orders_max,
                    stream_maxlen=stream_maxlen, src=src
                )
            except Exception as e:
                log.warn(COMPONENT, "poll_failed", err=str(e))
            await asyncio.wait_for(_stop.wait(), timeout=POLL_SEC)


def _install_signals():
    def _stop(_sig, _frm):
        try:
            _stop_event = _stop  # local shadow ok
        except Exception:
            return
        _stop.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, _stop)
        except Exception:
            pass


async def main():
    _install_signals()
    await sync_loop(PG_DSN)


if __name__ == "__main__":
    asyncio.run(main())
