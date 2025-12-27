import threading
from typing import Dict, Any, Optional, Iterable
from .snapshots import Snapshots
from .buffers import DoubleRingBuffer
from .models import SymbolMeta, SymbolState, SymbolFlags

class Registry:
    def __init__(self):
        self.snaps = Snapshots()
        self.trade_bufs: Dict[str, DoubleRingBuffer] = {}
        self.quote_bufs: Dict[str, DoubleRingBuffer] = {}
        self.meta: Dict[str, SymbolMeta] = {}
        self._lock = threading.RLock()

    def hydrate_symbols(self, symbols: Iterable[str], initial_states: Dict[str, SymbolState], flags: Dict[str, SymbolFlags]):
        with self._lock:
            for s in symbols:
                sU = s.upper()
                self.meta[sU] = SymbolMeta(symbol=sU, state=initial_states.get(sU, SymbolState.COLD), flags=flags.get(sU, SymbolFlags()))

    def set_flags(self, symbol: str, flags: SymbolFlags):
        with self._lock:
            m = self.meta.get(symbol.upper())
            if m:
                m.flags = flags

    def set_state(self, symbol: str, state: SymbolState):
        with self._lock:
            m = self.meta.get(symbol.upper())
            if m:
                m.state = state

    def push_trade(self, msg: Dict[str, Any]):
        sym = (msg.get("symbol") or "").upper()
        if not sym: return
        self.snaps.on_trade(msg)
        with self._lock:
            buf = self.trade_bufs.setdefault(sym, DoubleRingBuffer())
            buf.push(msg)
            m = self.meta.setdefault(sym, SymbolMeta(symbol=sym))
            m.last_price = msg.get("price")

    def push_quote(self, msg: Dict[str, Any]):
        sym = (msg.get("symbol") or "").upper()
        if not sym: return
        self.snaps.on_quote(msg)
        with self._lock:
            buf = self.quote_bufs.setdefault(sym, DoubleRingBuffer())
            buf.push(msg)
            m = self.meta.setdefault(sym, SymbolMeta(symbol=sym))
            m.last_bid = msg.get("bid")
            m.last_ask = msg.get("ask")

    def stat(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "symbols_total": len(self.meta),
                "symbols_trades": len(self.trade_bufs),
                "symbols_quotes": len(self.quote_bufs),
            }

    def get_meta(self, symbol: str) -> Optional[SymbolMeta]:
        with self._lock:
            return self.meta.get(symbol.upper())

    def all_meta(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            out = {}
            for s, m in self.meta.items():
                out[s] = {
                    "symbol": m.symbol,
                    "state": m.state.name,
                    "flags": { **({"no_trade": m.flags.no_trade, "halted": m.flags.halted, "restricted": m.flags.restricted}), **m.flags.custom },
                    "last_price": m.last_price,
                    "last_bid": m.last_bid,
                    "last_ask": m.last_ask,
                }
            return out
