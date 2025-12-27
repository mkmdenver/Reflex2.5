# Ingestor/worker.py
from __future__ import annotations

import json
import os
import signal
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, List, Optional, Tuple

import psycopg

from common.utils import load_env
from common import logging as log
from common.metrics_http import start_metrics_http, M  # M is the metrics registry/counters
from common.comm_garnet import garnet_rcli as rcli  # uses GARNET_URL

SERVICE   = os.getenv("REFLEX__SERVICE", "ingestor")
COMPONENT = __name__

# ----------------------------
# Environment & constants
# ----------------------------
DB_URL = (
    os.getenv("REFLEX__PG_DSN")
    or os.getenv("REFLEX__STORAGE__PG_DSN")
    or os.getenv("DATABASE_URL", "")
)
GARNET_URL = os.getenv("GARNET_URL", "redis://127.0.0.1:6379")
FLUSH_MS   = int(os.getenv("INGESTOR_FLUSH_MS", "250"))
BATCH_MAX  = int(os.getenv("INGESTOR_BATCH_MAX", "1000"))

# Only store bare-minimum columns to stay schema-safe:
#   ticks(symbol, timestamp, price)
#   quotes(symbol, timestamp, bid, bid_size, ask, ask_size)
SQL_INSERT_TICK = "INSERT INTO ticks(symbol, timestamp, price) VALUES (%s, %s, %s)"
SQL_INSERT_QUOTE = (
    "INSERT INTO quotes(symbol, timestamp, bid, bid_size, ask, ask_size) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)

DROP_LOG_EVERY = 5000  # throttle noisy logs on malformed payloads


# ----------------------------
# Helpers
# ----------------------------
def _metrics_http() -> None:
    """Start the tiny metrics server; port from env or default 7005."""
    port = int(os.getenv("INGESTOR_METRICS_PORT", "7005"))
    start_metrics_http(SERVICE, port)
    log.info(COMPONENT, "metrics_http_started", service=SERVICE, port=port)


def _to_decimal(x: Any, q: Optional[Decimal] = None) -> Optional[Decimal]:
    """Safe Decimal conversion; optionally quantize to a scale."""
    if x is None:
        return None
    try:
        d = Decimal(str(x))
        return d if q is None else d.quantize(q)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _epoch_to_ns(v: Any) -> Optional[int]:
    """Normalize epoch value to ns (handles ms/us/ns)."""
    try:
        iv = int(v)
    except Exception:
        return None
    if iv < 1_000_000_000_000:          # < 1e12 -> ms
        return iv * 1_000_000
    elif iv < 1_000_000_000_000_000:    # < 1e15 -> us
        return iv * 1_000
    else:                                # ns
        return iv


def _latency_ms_from_msg(msg: dict) -> Optional[float]:
    """Compute latency (now - sip/ts) in ms for metrics only."""
    src = msg.get("sip_timestamp", msg.get("ts"))
    now_ns = time.time_ns()
    src_ns = _epoch_to_ns(src)
    if src_ns is None:
        return None
    return max(0.0, (now_ns - src_ns) / 1_000_000.0)


# ----------------------------
# Row mappers (strict)
# ----------------------------
def map_trade(msg: dict) -> Optional[Tuple[str, datetime, Decimal]]:
    """
    Expect: symbol, price, ... (ignore extras).
    Drop if price is missing/invalid/zero.
    """
    sym = msg.get("symbol")
    price = _to_decimal(msg.get("price"))
    if not sym or price is None or price == 0:
        M.inc("ticks_dropped_noprice")
        if (M.get_counter("ticks_dropped_noprice") % DROP_LOG_EVERY) == 0:
            log.warn(COMPONENT, "dropping_trade_without_price", sample=msg)
        return None
    ts = datetime.now(timezone.utc)
    return (sym, ts, price)


def map_quote(msg: dict) -> Optional[Tuple[str, datetime, Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[Decimal]]]:
    """
    Accept either {bid, ask, bid_size, ask_size} or {bid_price, ask_price, ...}.
    Store None for missing sizes/prices; DB schema should allow NULLs on sizes if absent.
    """
    sym = msg.get("symbol")
    if not sym:
        M.inc("quotes_dropped_nosymbol")
        return None

    bid = msg.get("bid")
    ask = msg.get("ask")
    bid_p = msg.get("bid_price")
    ask_p = msg.get("ask_price")
    # prefer *_price if present
    bid_val = _to_decimal(bid_p if bid_p is not None else bid)
    ask_val = _to_decimal(ask_p if ask_p is not None else ask)

    bs = msg.get("bid_size")
    ask_s = msg.get("ask_size")
    bid_sz = _to_decimal(bs)
    ask_sz = _to_decimal(ask_s)

    ts = datetime.now(timezone.utc)
    return (sym, ts, bid_val, bid_sz, ask_val, ask_sz)


# ----------------------------
# Bulk writer
# ----------------------------
class BulkWriter:
    def __init__(self, conn: psycopg.Connection, batch_max: int = BATCH_MAX):
        self.conn = conn
        self.batch_max = batch_max
        self.tick_rows: List[Tuple[str, datetime, Decimal]] = []
        self.quote_rows: List[
            Tuple[str, datetime, Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[Decimal]]
        ] = []
        self.lock = threading.Lock()

    def add_tick(self, row: Tuple[str, datetime, Decimal]) -> None:
        with self.lock:
            self.tick_rows.append(row)

    def add_quote(self, row: Tuple[str, datetime, Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[Decimal]]) -> None:
        with self.lock:
            self.quote_rows.append(row)

    def should_flush(self) -> bool:
        with self.lock:
            return (len(self.tick_rows) + len(self.quote_rows)) >= self.batch_max

    def flush(self) -> None:
        with self.lock:
            ticks = self.tick_rows
            quotes = self.quote_rows
            self.tick_rows = []
            self.quote_rows = []

        if not ticks and not quotes:
            return

        t0 = time.time()
        try:
            with self.conn.pipeline():
                with self.conn.cursor() as cur:
                    if ticks:
                        cur.executemany(SQL_INSERT_TICK, ticks)
                    if quotes:
                        cur.executemany(SQL_INSERT_QUOTE, quotes)
            # success metrics
            if ticks:
                M.inc("ticks_written", len(ticks))
            if quotes:
                M.inc("quotes_written", len(quotes))
            M.inc("batches", 1)
        except Exception as e:
            log.warn(
                COMPONENT,
                "flush_error_dropping_batch",
                err=str(e),
                dropped_ticks=len(ticks),
                dropped_quotes=len(quotes),
            )
            # Optionally: M.inc("flush_errors")

        dt_ms = (time.time() - t0) * 1000.0
        M.set_gauge("flush_dt_ms", dt_ms)
        M.set_gauge("pending_ticks", len(self.tick_rows))
        M.set_gauge("pending_quotes", len(self.quote_rows))


# ----------------------------
# Main listen loop
# ----------------------------
_stop_flag = threading.Event()


def listen_and_persist() -> None:
    if not DB_URL:
        log.error(COMPONENT, "missing_database_url",
                  hint="set REFLEX__PG_DSN or REFLEX__STORAGE__PG_DSN or DATABASE_URL")
        raise SystemExit(2)

    # DB connection
    conn = psycopg.connect(DB_URL, autocommit=True)

    # batcher
    writer = BulkWriter(conn, batch_max=BATCH_MAX)

    # Redis/Garnet subscriber
    r = rcli()  # from common.comm_garnet; uses GARNET_URL
    p = r.pubsub(ignore_subscribe_messages=True)
    p.psubscribe("md.trades.*", "md.quotes.*")
    log.info(COMPONENT, "subscribed_channels", trades="md.trades.*", quotes="md.quotes.*")
    M.inc("subscribed")

    # flush timer
    last_flush = time.time()

    while not _stop_flag.is_set():
        try:
            msg = p.get_message(timeout=0.2)
            now = time.time()

            # periodic flush by time
            if (now - last_flush) * 1000.0 >= FLUSH_MS or writer.should_flush():
                writer.flush()
                last_flush = now

            if not msg:
                continue

            mtype = msg.get("type")
            if mtype not in ("message", "pmessage"):
                continue

            channel = msg.get("channel")
            data = msg.get("data")
            if isinstance(channel, bytes):
                channel = channel.decode("utf-8", "ignore")
            if isinstance(data, bytes):
                data = data.decode("utf-8", "ignore")

            # parse JSON payload
            try:
                payload = json.loads(data)
            except Exception:
                M.inc("json_errors")
                continue

            # latency (metrics only)
            lat = _latency_ms_from_msg(payload)
            if lat is not None:
                M.set_gauge("latency_ms_last", lat)

            # route by channel
            if channel.startswith("md.trades."):
                row = map_trade(payload)
                M.inc("ticks_in")
                if row is not None:
                    writer.add_tick(row)
            elif channel.startswith("md.quotes."):
                row = map_quote(payload)
                M.inc("quotes_in")
                if row is not None:
                    writer.add_quote(row)
            else:
                # ignore everything else
                pass

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.warn(COMPONENT, "listen_loop_error_continuing", err=str(e))
            time.sleep(0.05)

    # final flush
    try:
        writer.flush()
    except Exception:
        pass
    conn.close()


# ----------------------------
# Entrypoint
# ----------------------------
def _install_signals():
    def _stop(signum, frame):
        _stop_flag.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, _stop)
        except Exception:
            pass


def main():
    load_env()
    _metrics_http()
    log.info(COMPONENT, "starting_ingestor", flush_ms=FLUSH_MS, batch_max=BATCH_MAX)
    _install_signals()
    listen_and_persist()
    log.info(COMPONENT, "ingestor_stopping")

if __name__ == "__main__":
    main()
