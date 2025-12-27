from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
import pyarrow as pa, pyarrow.parquet as pq
from common.metrics import with_labels, parquet_rows_written, parquet_rowgroup_flush, parquet_write_seconds
from time import perf_counter

TRADE_SCHEMA = pa.schema([
    ("symbol", pa.string()),
    ("ts_ns", pa.int64()),
    ("price", pa.float64()),
    ("size", pa.int32()),
    ("exchange", pa.int16()),
    ("conditions", pa.list_(pa.int16())),
    ("seq", pa.int64()),
    ("participant_ts_ns", pa.int64())
])

QUOTE_SCHEMA = pa.schema([
    ("symbol", pa.string()),
    ("ts_ns", pa.int64()),
    ("bid_price", pa.float64()),
    ("bid_size", pa.int32()),
    ("ask_price", pa.float64()),
    ("ask_size", pa.int32()),
    ("bid_exchange", pa.int16()),
    ("ask_exchange", pa.int16()),
    ("conditions", pa.list_(pa.int16())),
    ("seq", pa.int64())
])

class _Writer:
    def __init__(self, root: Path, kind: str, schema: pa.Schema, target_rows=128_000):
        self.root = Path(root); self.kind = kind; self.schema = schema
        self._writer = None; self._path = None; self._rows = 0; self._target = target_rows

    def _path_for(self, symbol: str, ts_ns: int) -> Path:
        dt = datetime.fromtimestamp(ts_ns/1e9, tz=timezone.utc).strftime("%Y-%m-%d")
        p = self.root / f"sym={symbol}" / f"date={dt}"
        p.mkdir(parents=True, exist_ok=True)
        return p / f"{self.kind}-{dt}.parquet"

    def write_event(self, ev: dict):
        symbol = ev["symbol"]; ts_ns = int(ev["ts_ns"])
        path = self._path_for(symbol, ts_ns)
        if str(path) != str(self._path):
            self._close()
            self._writer = pq.ParquetWriter(path, self.schema, compression="zstd")
            self._path = path; self._rows = 0
        cols = [ev.get(n) for n in self.schema.names]
        batch = pa.record_batch([[c] for c in cols], schema=self.schema)
        t0 = perf_counter()
        self._writer.write_table(pa.Table.from_batches([batch], schema=self.schema))
        with_labels(parquet_rows_written).labels(kind=self.kind).inc()
        self._rows += 1
        if self._rows >= self._target:
            with_labels(parquet_rowgroup_flush).labels(kind=self.kind).inc()
            self._close()

    def _close(self):
        if self._writer:
            self._writer.close()
        self._writer = None
        self._path = None
        self._rows = 0

    def close(self):
        self._close()

class TradeWriter(_Writer):
    def __init__(self, root: Path):
        super().__init__(root, "trades", TRADE_SCHEMA)

class QuoteWriter(_Writer):
    def __init__(self, root: Path):
        super().__init__(root, "quotes", QUOTE_SCHEMA)
