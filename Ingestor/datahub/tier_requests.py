# datahub/tier_requests.py
from __future__ import annotations

import os
from typing import Literal

import psycopg
from common.bus import subscribe, CHANNELS, unpack
from common.logging import info, error

TierName = Literal["COLD", "WATCH", "WARM", "HOT"]

def _pg_connect():
    """
    Keep it simple: DSN first, then discrete env vars.
    This is the same DB DataHub uses for symbol_metadata.
    """
    dsn = os.getenv("REFLEX__PG_DSN")
    if dsn:
        return psycopg.connect(dsn, autocommit=True)

    host = os.getenv("POSTGRES_HOST", "localhost")
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    db   = os.getenv("POSTGRES_DB", "stock_data")
    user = os.getenv("POSTGRES_USER", "reflex")
    pwd  = os.getenv("POSTGRES_PASSWORD", "")
    return psycopg.connect(
        host=host,
        port=port,
        dbname=db,
        user=user,
        password=pwd,
        autocommit=True,
    )

class TierRequestListener:
    """
    Listens for *service-level* tier requests and applies them to symbol_metadata.rt_tier.

    Bus contract on CHANNELS["raise"]:
        { "symbol": "ASNS", "tier": "HOT", "source": "bullflag_v1", "ts": 123456789 }

    - Callers promise to be monotone (only raise, not lower).
    - We don't demote here; v2 can add per-source accounting.
    """

    async def run(self) -> None:
        ps = await subscribe(CHANNELS["raise"])
        info("hub", "tier_requests.listen.start")
        conn = _pg_connect()
        try:
            async for msg in ps.listen():
                if msg.get("type") != "message":
                    continue

                ev = unpack(msg["data"]) or {}
                sym = (ev.get("symbol") or "").upper()
                tier = (ev.get("tier") or "").upper()
                src  = ev.get("source") or "unknown"

                if not sym or tier not in ("COLD", "WATCH", "WARM", "HOT"):
                    continue

                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE symbol_metadata
                               SET rt_tier = %s,
                                   rt_tier_updated_at = NOW()
                             WHERE symbol = %s
                            """,
                            (tier, sym),
                        )
                    info("hub", "tier_request.applied", symbol=sym, tier=tier, source=src)
                except Exception as e:
                    error("hub", f"tier_request.apply_failed {sym} {tier}: {e}")
        finally:
            conn.close()
