# datahub/adapters/replay_adapter.py
from __future__ import annotations
import os, time, heapq, pathlib
from typing import Dict, Iterable, Iterator, List, Literal, Optional, Tuple, Any
import pandas as pd

Event = Dict[str, Any]
Kind = Literal["trades", "quotes"]

def _ns(v) -> int:
    # robust cast to int ns
    if pd.isna(v): return 0
    iv = int(v)
    return iv

class ReplayAdapter:
    """
    Parquet-backed market data replay:
      - No stubs. Fully runnable.
      - Loads per-symbol trades/quotes into iterators.
      - Merges across symbols by sip_timestamp (ns).
      - Optional wall-clock pacing via REPLAY_SPEED.
    Directory expectations (configurable by env):
      REPLAY_TRADES_DIR:   D:\replay\trades\\YYYY-MM-DD\\   (files like AAPL.parquet)
      REPLAY_QUOTES_DIR:   D:\replay\\quotes\\YYYY-MM-DD\\   (files like AAPL.parquet)
    Env knobs:
      REPLAY_DATE=YYYY-MM-DD
      REPLAY_SPEED=1.0     (1.0 = real-time; 10 = 10x; 0 or <0 => no sleeping)
      REPLAY_MAX_ROWS=0    (0 = all)
      REPLAY_TS_COL=sip_timestamp  (ns epoch col name)
    """
    def __init__(self,
                 root: Optional[str] = None,
                 date: Optional[str] = None,
                 trades_dir: Optional[str] = None,
                 quotes_dir: Optional[str] = None):
        self.root = root or os.getenv("REFLEX__STORAGE__PARQUET_ROOT") or os.getenv("PARQUET_ROOT", "")
        self.date = date or os.getenv("REPLAY_DATE", "")
        # Allow explicit per-kind directory
        self.trades_dir = trades_dir or os.getenv("REPLAY_TRADES_DIR") or ""
        self.quotes_dir = quotes_dir or os.getenv("REPLAY_QUOTES_DIR") or ""

        # Fallback layout: <root>\replay\<kind>\<YYYY-MM-DD>\
        if not self.trades_dir:
            self.trades_dir = os.path.join(self.root, "replay", "trades", self.date)
        if not self.quotes_dir:
            self.quotes_dir = os.path.join(self.root, "replay", "quotes", self.date)

        self.ts_col = os.getenv("REPLAY_TS_COL", "sip_timestamp")
        self.speed  = float(os.getenv("REPLAY_SPEED", "1.0"))
        self.max_rows = int(os.getenv("REPLAY_MAX_ROWS", "0"))

        self._open = False
        self._subs_trades: set[str] = set()
        self._subs_quotes: set[str] = set()
        self._iters_trades: Dict[str, Iterator[Event]] = {}
        self._iters_quotes: Dict[str, Iterator[Event]] = {}

        # merge heap: (ts_ns, seq, kind, symbol, event)
        self._heap: List[Tuple[int, int, str, str, Event]] = []
        self._seq = 0
        self._first_ts: Optional[int] = None
        self._first_wall: Optional[float] = None

    # lifecycle --------------------------------------------------------------
    def start(self) -> None:
        if self._open:
            return
        # Ensure folders exist (don’t crash; empty folder => no data)
        pathlib.Path(self.trades_dir).mkdir(parents=True, exist_ok=True)
        pathlib.Path(self.quotes_dir).mkdir(parents=True, exist_ok=True)
        self._open = True

    def close(self) -> None:
        self._open = False
        self._subs_trades.clear()
        self._subs_quotes.clear()
        self._iters_trades.clear()
        self._iters_quotes.clear()
        self._heap.clear()
        self._seq = 0
        self._first_ts = None
        self._first_wall = None

    def is_open(self) -> bool:
        return self._open

    # subscribe / unsubscribe -----------------------------------------------
    def subscribe(self, trades: List[str] | None = None, quotes: List[str] | None = None) -> dict:
        if not self.is_open():
            self.start()
        trades = list(trades or [])
        quotes = list(quotes or [])
        # Load newly requested symbols
        newly_t = [s for s in trades if s not in self._subs_trades]
        newly_q = [s for s in quotes if s not in self._subs_quotes]
        if newly_t:
            self._load_kind_symbols(kind="trades", symbols=newly_t)
            self._subs_trades.update(newly_t)
        if newly_q:
            self._load_kind_symbols(kind="quotes", symbols=newly_q)
            self._subs_quotes.update(newly_q)
        return {"trades": trades, "quotes": quotes}

    def unsubscribe(self, trades: List[str] | None = None, quotes: List[str] | None = None) -> dict:
        trades = list(trades or [])
        quotes = list(quotes or [])
        for s in trades:
            self._subs_trades.discard(s)
            self._iters_trades.pop(s, None)
        for s in quotes:
            self._subs_quotes.discard(s)
            self._iters_quotes.pop(s, None)
        # Note: entries already in heap may still drain; that’s fine for replay
        return {"trades": trades, "quotes": quotes}

    # streaming --------------------------------------------------------------
    def stream(self) -> Iterable[Event]:
        """
        Emit events merged by ns timestamp across all subscribed symbols/kinds.
        If REPLAY_SPEED <= 0, no sleeping (fast-forward).
        Otherwise sleep to approximate (ts_delta / REPLAY_SPEED).
        """
        # prime heap from any existing iters (subscribe may have preloaded)
        self._prime_heap_from_iters()

        while self._heap:
            ts_ns, _, kind, sym, ev = heapq.heappop(self._heap)

            # pacing
            if self.speed > 0:
                if self._first_ts is None:
                    self._first_ts = ts_ns
                    self._first_wall = time.perf_counter()
                else:
                    elapsed_ns = ts_ns - self._first_ts
                    target_s = (elapsed_ns / 1_000_000_000.0) / self.speed
                    now_s = time.perf_counter() - (self._first_wall or 0.0)
                    if target_s > now_s:
                        time.sleep(target_s - now_s)

            yield ev

            # push next from that iterator
            it = None
            if kind == "trades":
                it = self._iters_trades.get(sym)
            else:
                it = self._iters_quotes.get(sym)
            if it is not None:
                try:
                    nxt = next(it)
                    self._push(kind, sym, nxt)
                except StopIteration:
                    # iterator exhausted
                    if kind == "trades":
                        self._iters_trades.pop(sym, None)
                    else:
                        self._iters_quotes.pop(sym, None)

    # internals --------------------------------------------------------------
    def _prime_heap_from_iters(self) -> None:
        for sym, it in list(self._iters_trades.items()):
            try:
                ev = next(it)
                self._push("trades", sym, ev)
            except StopIteration:
                self._iters_trades.pop(sym, None)
        for sym, it in list(self._iters_quotes.items()):
            try:
                ev = next(it)
                self._push("quotes", sym, ev)
            except StopIteration:
                self._iters_quotes.pop(sym, None)

    def _push(self, kind: str, sym: str, ev: Event) -> None:
        ts_ns = _ns(ev.get(self.ts_col, 0))
        self._seq += 1
        heapq.heappush(self._heap, (ts_ns, self._seq, kind, sym, ev))

    def _load_kind_symbols(self, kind: str, symbols: List[str]) -> None:
        base = self.trades_dir if kind == "trades" else self.quotes_dir
        for sym in symbols:
            path = os.path.join(base, f"{sym}.parquet")
            if not os.path.exists(path):
                # not fatal—symbol just has no data that day
                continue
            df = pd.read_parquet(path, engine="pyarrow")
            # normalize ts and shape to common event dicts
            if self.ts_col not in df.columns:
                # fallbacks: ts_ns, participant_timestamp, or ts
                for alt in ("ts_ns", "participant_timestamp", "ts"):
                    if alt in df.columns:
                        df[self.ts_col] = df[alt].astype("int64")
                        break
                if self.ts_col not in df.columns:
                    # synthesize from index if totally missing
                    df[self.ts_col] = pd.Series(range(len(df)), dtype="int64") * 1_000_000

            cols = set(df.columns)
            keep_cols = {self.ts_col}
            # project a minimal field set
            if kind == "trades":
                keep_cols |= set(c for c in ("price","size","conditions","exchange") if c in cols)
            else:
                keep_cols |= set(c for c in ("bid_price","bid_size","ask_price","ask_size","conditions","exchange") if c in cols)
            # include symbol
            df = df[list(keep_cols)].copy()
            df["symbol"] = sym
            df["kind"] = kind
            # sort and trim
            df.sort_values(self.ts_col, inplace=True)
            if self.max_rows > 0 and len(df) > self.max_rows:
                df = df.iloc[: self.max_rows]
            # turn into iterator of dicts
            it = (row._asdict() if hasattr(row, "_asdict") else dict(row) for _, row in df.iterrows())
            if kind == "trades":
                self._iters_trades[sym] = it
            else:
                self._iters_quotes[sym] = it
