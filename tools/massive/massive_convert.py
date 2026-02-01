import argparse
import csv
import datetime as dt
import gzip
import os
import sys
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Set, Tuple

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    print("PyArrow not found. Installing...")
    import subprocess
    import sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyarrow"])
    import pyarrow as pa
    import pyarrow.parquet as pq


# ----------------------------
# Config / defaults
# ----------------------------

DEFAULT_FLAT_BASE = r"D:\polygon_flat\us_stocks_sip"
DEFAULT_OUT_TICKS = r"D:\reflex_parquet2"   # change if you want a different root
DEFAULT_OUT_QUOTES = r"D:\reflex_parquet2\_quotes"  # change if you want a different root


# ----------------------------
# Helpers
# ----------------------------

def parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def read_universe(path: str) -> Set[str]:
    syms: Set[str] = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            t = line.strip()
            if not t or t.startswith("#"):
                continue
            syms.add(t.upper())
    return syms

def flat_path(flat_base: str, dataset: str, day: dt.date) -> str:
    # dataset is "trades_v1" or "quotes_v1"
    return os.path.join(flat_base, dataset, f"{day.year:04d}", f"{day.month:02d}", f"{day.isoformat()}.csv.gz")

def out_parquet_path(out_root: str, kind: str, symbol: str, day: dt.date) -> str:
    # KISS: D:\reflex_parquet\AAPL\AAPL_2025-12-01.parquet  (ticks)
    # For quotes we can do: D:\reflex_parquet\_quotes\AAPL\AAPL_2025-12-01.parquet
    if kind == "ticks":
        d = os.path.join(out_root, symbol)
        ensure_dir(d)
        return os.path.join(d, f"{symbol}_{day.isoformat()}.parquet")
    else:
        d = os.path.join(out_root, "_quotes", symbol)
        ensure_dir(d)
        return os.path.join(d, f"{symbol}_{day.isoformat()}.parquet")

def _try_int(x: str) -> Optional[int]:
    if x == "" or x is None:
        return None
    try:
        return int(x)
    except Exception:
        return None

def _try_float(x: str) -> Optional[float]:
    if x == "" or x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None


# ----------------------------
# Conversion: TRADES
# ----------------------------

def convert_trades_day(
    day: dt.date,
    universe: Set[str],
    flat_base: str,
    out_root: str,
    flush_rows: int = 200_000,
    overwrite: bool = False,
) -> Dict[str, int]:
    """
    Convert one day of trades_v1 flat file into per-symbol parquet files.
    Returns {symbol: rows_written}.
    """

    src = flat_path(flat_base, "trades_v1", day)
    if not os.path.exists(src):
        raise FileNotFoundError(src)

    # Accumulate in-memory chunks per symbol; flush to parquet periodically
    buffers: Dict[str, Dict[str, List]] = {}
    counts: Dict[str, int] = defaultdict(int)

    def buf_for(sym: str) -> Dict[str, List]:
        b = buffers.get(sym)
        if b is None:
            b = {
                "symbol": [],
                "timestamp": [],  # ns epoch
                "sip_timestamp": [],
                "participant_timestamp": [],
                "trf_timestamp": [],
                "price": [],
                "size": [],
                "exchange": [],
                "conditions": [],  # leave as string; you can parse later if needed
                "tape": [],
                "trade_id": [],
            }
            buffers[sym] = b
        return b

    def flush(sym: str) -> None:
        b = buffers.get(sym)
        if not b or len(b["timestamp"]) == 0:
            return

        out_path = out_parquet_path(out_root, "ticks", sym, day)
        if (not overwrite) and os.path.exists(out_path):
            # If file exists, append by writing another file is annoying;
            # simplest: overwrite must be true when re-running.
            # We'll default to overwrite=False but will stop here to avoid silently duplicating.
            raise RuntimeError(f"Parquet exists (set --overwrite to rebuild): {out_path}")

        # Build Arrow table
        table = pa.table({
            "symbol": pa.array(b["symbol"], pa.string()),
            "timestamp_ns": pa.array(b["timestamp"], pa.int64()),
            "sip_timestamp": pa.array(b["sip_timestamp"], pa.int64()),
            "participant_timestamp": pa.array(b["participant_timestamp"], pa.int64()),
            "trf_timestamp": pa.array(b["trf_timestamp"], pa.int64()),
            "price": pa.array(b["price"], pa.float64()),
            "size": pa.array(b["size"], pa.int32()),
            "exchange": pa.array(b["exchange"], pa.int32()),
            "conditions": pa.array(b["conditions"], pa.string()),
            "tape": pa.array(b["tape"], pa.int32()),
            "trade_id": pa.array(b["trade_id"], pa.string()),
        })

        pq.write_table(table, out_path, compression="zstd")
        counts[sym] += table.num_rows

        # Reset
        for k in b.keys():
            b[k].clear()

    # Stream CSV
    with gzip.open(src, "rt", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        required = ["ticker", "participant_timestamp", "sip_timestamp", "trf_timestamp", "price", "size", "exchange", "conditions", "tape", "id"]
        # Massive/Polygon column names can vary slightly; we’ll map robustly.
        # We'll detect by presence of common keys:
        header = reader.fieldnames or []
        # Known variants:
        # ticker or symbol, participant_timestamp may be participant_timestamp, etc.

        def get(row, *names):
            for n in names:
                if n in row:
                    return row.get(n, "")
            return ""

        n = 0
        for row in reader:
            n += 1
            sym = (get(row, "ticker", "symbol") or "").upper()
            if sym not in universe:
                continue

            # timestamps in flat files are typically nanoseconds since epoch
            sip_ts = _try_int(get(row, "sip_timestamp"))
            part_ts = _try_int(get(row, "participant_timestamp"))
            trf_ts = _try_int(get(row, "trf_timestamp"))

            # Choose a canonical timestamp for ordering:
            # prefer sip_timestamp, else participant, else trf.
            ts = sip_ts or part_ts or trf_ts
            if ts is None:
                continue

            b = buf_for(sym)
            b["symbol"].append(sym)
            b["timestamp"].append(ts)
            b["sip_timestamp"].append(sip_ts)
            b["participant_timestamp"].append(part_ts)
            b["trf_timestamp"].append(trf_ts)
            b["price"].append(_try_float(get(row, "price")) or 0.0)
            b["size"].append(_try_int(get(row, "size")) or 0)
            b["exchange"].append(_try_int(get(row, "exchange")) or 0)
            b["conditions"].append(get(row, "conditions") or "")
            b["tape"].append(_try_int(get(row, "tape")) or 0)
            b["trade_id"].append(get(row, "id", "trade_id") or "")

            # Flush when buffer gets big
            if len(b["timestamp"]) >= flush_rows:
                flush(sym)

    # Final flush
    for sym in list(buffers.keys()):
        if len(buffers[sym]["timestamp"]) > 0:
            flush(sym)

    return dict(counts)


# ----------------------------
# Conversion: QUOTES
# ----------------------------

def convert_quotes_day(
    day: dt.date,
    universe: Set[str],
    flat_base: str,
    out_root: str,
    flush_rows: int = 300_000,
    overwrite: bool = False,
) -> Dict[str, int]:
    """
    Convert one day of quotes_v1 flat file into per-symbol parquet files under _quotes.
    Returns {symbol: rows_written}.
    """

    src = flat_path(flat_base, "quotes_v1", day)
    if not os.path.exists(src):
        raise FileNotFoundError(src)

    buffers: Dict[str, Dict[str, List]] = {}
    counts: Dict[str, int] = defaultdict(int)

    def buf_for(sym: str) -> Dict[str, List]:
        b = buffers.get(sym)
        if b is None:
            b = {
                "symbol": [],
                "timestamp": [],  # ns epoch (sip_timestamp preferred)
                "sip_timestamp": [],
                "participant_timestamp": [],
                "bid_price": [],
                "bid_size": [],
                "bid_exchange": [],
                "ask_price": [],
                "ask_size": [],
                "ask_exchange": [],
                "conditions": [],
                "tape": [],
            }
            buffers[sym] = b
        return b

    def flush(sym: str) -> None:
        b = buffers.get(sym)
        if not b or len(b["timestamp"]) == 0:
            return

        out_path = out_parquet_path(out_root, "quotes", sym, day)
        if (not overwrite) and os.path.exists(out_path):
            raise RuntimeError(f"Parquet exists (set --overwrite to rebuild): {out_path}")

        table = pa.table({
            "symbol": pa.array(b["symbol"], pa.string()),
            "timestamp_ns": pa.array(b["timestamp"], pa.int64()),
            "sip_timestamp": pa.array(b["sip_timestamp"], pa.int64()),
            "participant_timestamp": pa.array(b["participant_timestamp"], pa.int64()),
            "bid_price": pa.array(b["bid_price"], pa.float64()),
            "bid_size": pa.array(b["bid_size"], pa.int32()),
            "bid_exchange": pa.array(b["bid_exchange"], pa.int32()),
            "ask_price": pa.array(b["ask_price"], pa.float64()),
            "ask_size": pa.array(b["ask_size"], pa.int32()),
            "ask_exchange": pa.array(b["ask_exchange"], pa.int32()),
            "conditions": pa.array(b["conditions"], pa.string()),
            "tape": pa.array(b["tape"], pa.int32()),
        })

        pq.write_table(table, out_path, compression="zstd")
        counts[sym] += table.num_rows

        for k in b.keys():
            b[k].clear()

    with gzip.open(src, "rt", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)

        def get(row, *names):
            for n in names:
                if n in row:
                    return row.get(n, "")
            return ""

        for row in reader:
            sym = (get(row, "ticker", "symbol") or "").upper()
            if sym not in universe:
                continue

            sip_ts = _try_int(get(row, "sip_timestamp"))
            part_ts = _try_int(get(row, "participant_timestamp"))
            ts = sip_ts or part_ts
            if ts is None:
                continue

            b = buf_for(sym)
            b["symbol"].append(sym)
            b["timestamp"].append(ts)
            b["sip_timestamp"].append(sip_ts)
            b["participant_timestamp"].append(part_ts)

            b["bid_price"].append(_try_float(get(row, "bid_price")) or 0.0)
            b["bid_size"].append(_try_int(get(row, "bid_size")) or 0)
            b["bid_exchange"].append(_try_int(get(row, "bid_exchange")) or 0)

            b["ask_price"].append(_try_float(get(row, "ask_price")) or 0.0)
            b["ask_size"].append(_try_int(get(row, "ask_size")) or 0)
            b["ask_exchange"].append(_try_int(get(row, "ask_exchange")) or 0)

            b["conditions"].append(get(row, "conditions") or "")
            b["tape"].append(_try_int(get(row, "tape")) or 0)

            if len(b["timestamp"]) >= flush_rows:
                flush(sym)

    for sym in list(buffers.keys()):
        if len(buffers[sym]["timestamp"]) > 0:
            flush(sym)

    return dict(counts)


# ----------------------------
# CLI
# ----------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Convert Massive/Polygon SIP flat files (.csv.gz) to per-symbol parquet.")
    ap.add_argument("--day", required=True, help="Day YYYY-MM-DD (must exist in flat staging)")
    ap.add_argument("--universe", required=True, help="Path to universe file (one symbol per line)")
    ap.add_argument("--flat-base", default=DEFAULT_FLAT_BASE, help=f"Flat files base dir (default: {DEFAULT_FLAT_BASE})")
    ap.add_argument("--out-root", default=DEFAULT_OUT_TICKS, help=f"Output parquet root (default: {DEFAULT_OUT_TICKS})")
    ap.add_argument("--dataset", choices=["trades", "quotes", "both"], default="both", help="What to convert")
    ap.add_argument("--flush-rows", type=int, default=200_000, help="Rows per symbol buffer before flush")
    ap.add_argument("--overwrite", action="store_true", help="Overwrite existing parquet day files (recommended for reseal)")
    args = ap.parse_args()

    day = parse_date(args.day)
    universe = read_universe(args.universe)
    print(f"[INFO] Universe symbols: {len(universe)}")

    if args.dataset in ("trades", "both"):
        print(f"[INFO] Converting trades for {day.isoformat()} ...")
        counts = convert_trades_day(
            day=day,
            universe=universe,
            flat_base=args.flat_base,
            out_root=args.out_root,
            flush_rows=args.flush_rows,
            overwrite=args.overwrite,
        )
        print(f"[DONE] trades symbols written: {len(counts)}  total_rows={sum(counts.values())}")

    if args.dataset in ("quotes", "both"):
        print(f"[INFO] Converting quotes for {day.isoformat()} ...")
        counts = convert_quotes_day(
            day=day,
            universe=universe,
            flat_base=args.flat_base,
            out_root=args.out_root,
            flush_rows=max(args.flush_rows, 300_000),
            overwrite=args.overwrite,
        )
        print(f"[DONE] quotes symbols written: {len(counts)}  total_rows={sum(counts.values())}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
