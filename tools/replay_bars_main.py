# tools/replay_bars_main.py
"""
Replay 1-minute bars from Postgres (minute_bars) into the REPLAY bars pub channel.

Hard rules:
- Always publish to REFLEX_DATAHUB_BARS1M_PUB_REPLAY (unless --channel override).
- Auto-load ROOT\.env and ROOT\.env.local (ROOT = repo root).
- Use venv python from the BAT; this file is just the runner.

Env:
- REFLEX_PG_DSN (or DATABASE_URL)
- REFLEX_DATAHUB_BARS1M_PUB_REPLAY (required unless --channel)

Args:
- --symbol BNAI
- --date 2025-08-09
- --start 09:30
- --end 16:00
- --speed 0   (0=as fast as possible; 1=realtime; 10=10x)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, date, time as dtime, timezone
from pathlib import Path
from typing import Optional

import psycopg

# --- repo root bootstrap + dotenv load (must happen before importing common.*) ---
_THIS = Path(__file__).resolve()
_repo_root: Optional[Path] = None
for p in [_THIS.parent, *_THIS.parents]:
    if (p / ".env").exists():
        _repo_root = p
        break
if _repo_root is None:
    _repo_root = _THIS.parents[1]

if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))


def _load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


_load_dotenv_file(_repo_root / ".env")
_load_dotenv_file(_repo_root / ".env.local")

from common.bus import publisher, pack  # noqa: E402


def _parse_hhmm(s: str, default: dtime) -> dtime:
    s = (s or "").strip()
    if not s:
        return default
    try:
        hh, mm = s.split(":")
        return dtime(hour=int(hh), minute=int(mm))
    except Exception:
        return default


def _parse_ymd(s: str) -> date:
    try:
        y, m, d = s.strip().split("-")
        return date(int(y), int(m), int(d))
    except Exception:
        raise SystemExit(f"[ERROR] Bad date (expected YYYY-MM-DD): {s!r}")


def _dt_utc(d: date, t: dtime) -> datetime:
    # Treat HH:MM as a session clock and convert to a tz-aware UTC datetime.
    return datetime(d.year, d.month, d.day, t.hour, t.minute, tzinfo=timezone.utc)


def _dsn() -> str:
    dsn = (os.getenv("REFLEX_PG_DSN") or os.getenv("DATABASE_URL") or "").strip()
    if not dsn:
        raise SystemExit("[ERROR] Missing REFLEX_PG_DSN (or DATABASE_URL) in environment.")
    return dsn


def _replay_bars_channel(override: str) -> str:
    if override:
        return override
    ch = (os.getenv("REFLEX_DATAHUB_BARS1M_PUB_REPLAY") or "").strip()
    if not ch:
        raise SystemExit("[ERROR] Missing REFLEX_DATAHUB_BARS1M_PUB_REPLAY in environment (or pass --channel).")
    return ch


def _fetch_bars(dsn: str, symbol: str, start_utc: datetime, end_utc: datetime) -> list[dict]:
    print(f"[replay_bars] fetching bars for {symbol} from {start_utc} to {end_utc}")
    """
    Schema (from your schema.sql):
      symbol, timestamp, open, high, low, close, volume
    """
    sym = symbol.upper()
    sql = """
        SELECT "timestamp", open, high, low, close, volume
        FROM minute_bars
        WHERE symbol = %s AND "timestamp" >= %s AND "timestamp" <= %s
        ORDER BY "timestamp" ASC
    """

    print(f"[replay_bars] executing SQL: {sql.strip()} with params: {sym}, {start_utc}, {end_utc}") 
    
    bars: list[dict] = []
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (sym, start_utc, end_utc))
            for ts, o, h, l, c, v in cur.fetchall():
                bars.append(
                    {
                        "sym": sym,
                        "t": int(ts.replace(tzinfo=timezone.utc).timestamp() * 1000),  # ms epoch
                        "o": float(o) if o is not None else float(c),
                        "h": float(h) if h is not None else float(c),
                        "l": float(l) if l is not None else float(c),
                        "c": float(c),
                        "v": int(v or 0),
                    }
                )
    return bars


async def _publish_bars(channel: str, bars: list[dict], speed: float) -> None:
    pub = await publisher()
    total = len(bars)
    if total == 0:
        print("[replay_bars] no bars to publish.")
        return

    print(f"[replay_bars] publishing {total} bars -> {channel}")
    print(f"[replay_bars] speed={speed} (0=fast, 1=realtime, 10=10x)")

    delay = 0.0 if speed <= 0 else (60.0 / max(speed, 0.001))

    sent = 0
    for bar in bars:
        await pub.publish(channel, pack(bar))
        sent += 1
        if sent % 100 == 0:
            print(f"[replay_bars] sent {sent}/{total} ...")
        if delay > 0:
            await asyncio.sleep(delay)

    print(f"[replay_bars] done. sent={sent}")


async def main() -> None:
    ap = argparse.ArgumentParser(description="Replay 1m bars from minute_bars into the REPLAY bars channel.")
    ap.add_argument("--symbol", required=True, help="Symbol to replay.")
    ap.add_argument("--date", required=True, help="Session date YYYY-MM-DD.")
    ap.add_argument("--start", default="09:30", help="Start HH:MM (default 09:30).")
    ap.add_argument("--end", default="16:00", help="End HH:MM (default 16:00).")
    ap.add_argument("--speed", default="0", help="Replay speed: 0=fast, 1=realtime, 10=10x.")
    ap.add_argument("--channel", default="", help="Override replay bars channel (default env REFLEX_DATAHUB_BARS1M_PUB_REPLAY).")
    args = ap.parse_args()

    symbol = args.symbol.strip().upper()
    d = _parse_ymd(args.date.strip())
    t0 = _parse_hhmm(args.start, dtime(9, 30))
    t1 = _parse_hhmm(args.end, dtime(16, 0))
    start_utc = _dt_utc(d, t0)
    end_utc = _dt_utc(d, t1)

    try:
        speed = float((args.speed or "0").strip())
    except Exception:
        speed = 0.0

    channel = _replay_bars_channel(args.channel.strip())

    print(f"[replay_bars] ROOT={_repo_root}")
    print(f"[replay_bars] symbol={symbol} date={d} start={t0} end={t1}")
    print(f"[replay_bars] channel={channel}")

    bars = _fetch_bars(_dsn(), symbol, start_utc, end_utc)
    await _publish_bars(channel, bars, speed)


if __name__ == "__main__":
    asyncio.run(main())
