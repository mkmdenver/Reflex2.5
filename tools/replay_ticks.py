# tools/replay_ticks_main.py
"""
Replay Polygon tick/quote Parquet into Trader's LIVE market-data pubsub channels.

Goal:
  - Provide a deterministic "RT-like" harness:
      replay_ticks_main -> publishes ticks/quotes (paced)
      intent_replayer --pace -> publishes intents (paced)
    Trader processes both through the normal RT path.

Channels:
  - Uses common.bus.CHANNELS:
      ticks:  CHANNELS["ticks_live"] or CHANNELS["ticks"] or "hub.ticks"
      quotes: CHANNELS["quotes_live"] or CHANNELS["quotes"] or "hub.quotes"

Parquet:
  - Requires pyarrow.
  - Handles a variety of Polygon-ish column conventions:
      symbol/sym
      price/p
      size/s
      exchange/x
      ts/t/timestamp/sip_timestamp/participant_timestamp/trf_timestamp
      quote fields: bp/bs/ap/as or bid/bid_size/ask/ask_size

Pacing:
  --speed 0    : as fast as possible
  --speed 1    : realtime
  --speed 10   : 10x
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from typing import Any, Dict, Optional, Iterable

# env bootstrap (match project conventions)
try:
    from trader.envload import load_dotenv_if_needed  # type: ignore
except Exception:
    load_dotenv_if_needed = None  # type: ignore

try:
    from common.bus import CHANNELS, publisher, pack  # type: ignore
except Exception:
    CHANNELS = {}  # type: ignore
    publisher = None  # type: ignore
    pack = None  # type: ignore


def _now() -> float:
    return time.time()


def _load_env() -> None:
    if load_dotenv_if_needed is None:
        return
    load_dotenv_if_needed()


def _as_float(x: Any) -> Optional[float]:
    try:
        if x is None or isinstance(x, bool):
            return None
        return float(x)
    except Exception:
        return None


def _as_int(x: Any) -> Optional[int]:
    try:
        if x is None or isinstance(x, bool):
            return None
        return int(x)
    except Exception:
        return None


def _coerce_epoch_seconds(v: Any) -> Optional[float]:
    """
    Accept:
      - seconds epoch (float)
      - ms epoch (int)
      - ns epoch (int)
    Heuristic by magnitude.
    """
    x = _as_float(v)
    if x is None:
        return None

    # magnitude heuristics
    if x > 1e17:  # ns
        return x / 1e9
    if x > 1e14:  # us
        return x / 1e6
    if x > 1e11:  # ms
        return x / 1e3
    return x


def _ticks_channel() -> str:
    return CHANNELS.get("ticks_live", CHANNELS.get("ticks", "hub.ticks"))  # type: ignore[arg-type]


def _quotes_channel() -> str:
    return CHANNELS.get("quotes_live", CHANNELS.get("quotes", "hub.quotes"))  # type: ignore[arg-type]


def _pick_ts(row: Dict[str, Any]) -> Optional[float]:
    for k in ("ts", "t", "timestamp", "sip_timestamp", "participant_timestamp", "trf_timestamp"):
        if k in row:
            t = _coerce_epoch_seconds(row.get(k))
            if t is not None:
                return t
    return None


def _row_to_tick(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    sym = (row.get("symbol") or row.get("sym") or "")
    sym = str(sym).upper().strip()
    if not sym:
        return None

    price = _as_float(row.get("price") if "price" in row else row.get("p"))
    size = _as_int(row.get("size") if "size" in row else row.get("s"))
    if price is None or size is None:
        return None

    out: Dict[str, Any] = {
        "symbol": sym,
        "p": float(price),
        "s": int(size),
    }
    exch = _as_int(row.get("exchange") if "exchange" in row else row.get("x"))
    if exch is not None:
        out["x"] = int(exch)

    ts = _pick_ts(row)
    if ts is not None:
        out["t"] = float(ts)
    return out


def _row_to_quote(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    sym = (row.get("symbol") or row.get("sym") or "")
    sym = str(sym).upper().strip()
    if not sym:
        return None

    bid = _as_float(row.get("bid") if "bid" in row else row.get("bp"))
    ask = _as_float(row.get("ask") if "ask" in row else row.get("ap"))
    if bid is None and ask is None:
        return None

    out: Dict[str, Any] = {"symbol": sym}
    if bid is not None:
        out["bp"] = float(bid)
    if ask is not None:
        out["ap"] = float(ask)

    bs = _as_int(row.get("bid_size") if "bid_size" in row else row.get("bs"))
    aS = _as_int(row.get("ask_size") if "ask_size" in row else row.get("as"))
    if bs is not None:
        out["bs"] = int(bs)
    if aS is not None:
        out["as"] = int(aS)

    ts = _pick_ts(row)
    if ts is not None:
        out["t"] = float(ts)

    return out


def _iter_parquet_rows(path: str, columns: Optional[list[str]] = None) -> Iterable[Dict[str, Any]]:
    """
    Stream parquet rows using pyarrow batches to avoid loading entire file.

    Requires pyarrow installed in your venv.
    """
    try:
        import pyarrow.parquet as pq  # type: ignore
    except Exception as exc:
        raise RuntimeError("pyarrow is required for replay_ticks_main.py (pip install pyarrow)") from exc

    pf = pq.ParquetFile(path)
    for batch in pf.iter_batches(batch_size=50_000, columns=columns):
        # Convert batch columns to python lists, then row dicts.
        cols = batch.schema.names
        arrays = [batch.column(i).to_pylist() for i in range(batch.num_columns)]
        for i in range(batch.num_rows):
            row = {cols[c]: arrays[c][i] for c in range(len(cols))}
            yield row


async def main() -> int:
    _load_env()

    ap = argparse.ArgumentParser(description="Replay tick/quote parquet into Trader md channels.")
    ap.add_argument("parquet", help="Path to a Polygon parquet file (ticks or quotes).")
    ap.add_argument("--mode", default="auto", choices=("auto", "ticks", "quotes", "both"), help="What to emit.")
    ap.add_argument("--speed", type=float, default=0.0, help="0=fast, 1=realtime, 10=10x.")
    ap.add_argument("--max", type=int, default=0, help="Max rows to publish (0=all).")
    ap.add_argument("--skip", type=int, default=0, help="Skip first N rows.")
    ap.add_argument("--log-every", type=int, default=20000, help="Progress log every N rows.")
    args = ap.parse_args()

    if publisher is None or pack is None:
        print("[ERROR] common.bus.publisher/pack unavailable; cannot publish.", file=sys.stderr)
        return 2

    path = args.parquet
    if not os.path.exists(path):
        print(f"[ERROR] File not found: {path}", file=sys.stderr)
        return 2

    ticks_ch = _ticks_channel()
    quotes_ch = _quotes_channel()

    pub = await publisher()

    print("[START] replay_ticks_main")
    print(f"[INFO] file={os.path.abspath(path)}")
    print(f"[INFO] ticks_channel={ticks_ch}")
    print(f"[INFO] quotes_channel={quotes_ch}")
    print(f"[INFO] mode={args.mode} speed={args.speed} (0=fast)")

    first_event_ts: Optional[float] = None
    wall_start = _now()
    sent_ticks = 0
    sent_quotes = 0
    seen = 0

    # auto mode: try to infer based on presence of quote-ish fields in first rows
    want_ticks = args.mode in ("auto", "ticks", "both")
    want_quotes = args.mode in ("auto", "quotes", "both")

    for row in _iter_parquet_rows(path):
        seen += 1
        if args.skip and seen <= int(args.skip):
            continue
        if args.max and (sent_ticks + sent_quotes) >= int(args.max):
            break

        # build payload(s)
        tick = _row_to_tick(row) if want_ticks else None
        quote = _row_to_quote(row) if want_quotes else None

        # refine auto: if we never produce ticks, allow quotes; if never quotes, allow ticks
        if args.mode == "auto":
            if tick is None:
                want_ticks = False
            if quote is None:
                want_quotes = False
            if not want_ticks and not want_quotes:
                # if row didn’t match either shape, keep scanning
                want_ticks = True
                want_quotes = True
                continue
            # after first match, lock
            args.mode = "both" if (want_ticks and want_quotes) else ("ticks" if want_ticks else "quotes")

        # pacing based on payload time
        payload_for_ts = tick or quote
        if args.speed and float(args.speed) > 0 and payload_for_ts:
            et = _coerce_epoch_seconds(payload_for_ts.get("t")) or _coerce_epoch_seconds(payload_for_ts.get("ts"))
            if et is not None:
                if first_event_ts is None:
                    first_event_ts = float(et)
                    wall_start = _now()
                replay_offset = max(0.0, float(et) - float(first_event_ts))
                target_wall = wall_start + (replay_offset / max(float(args.speed), 1e-9))
                delay = target_wall - _now()
                if delay > 0:
                    await asyncio.sleep(delay)

        if tick:
            await pub.publish(ticks_ch, pack(tick))
            sent_ticks += 1

        if quote:
            await pub.publish(quotes_ch, pack(quote))
            sent_quotes += 1

        if args.log_every and (seen % int(args.log_every) == 0):
            print(f"[PROGRESS] rows_seen={seen} sent_ticks={sent_ticks} sent_quotes={sent_quotes}")

    print(f"[DONE] rows_seen={seen} sent_ticks={sent_ticks} sent_quotes={sent_quotes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
