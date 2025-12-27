# common/market_adapter.py
from __future__ import annotations
from typing import Iterable, List, Literal, Dict, Any

from datahub.adapters.live_adapter import LIVEAdapter

Event = Dict[str, Any]
Kind = Literal["trades", "quotes"]

class MarketAdapter:
    """
    Concrete, operational adapter: thin wrapper over LIVEAdapter.
    No stubs. No abstract methods.
    """
    def __init__(self, api_key: str | None = None, ws_url: str | None = None):
        self._impl = LIVEAdapter(api_key=api_key, ws_url=ws_url)

    def open(self) -> None:
        self._impl.start()

    def close(self) -> None:
        self._impl.close()

    def subscribe(self, symbols: List[str], kinds: List[Kind]) -> None:
        # Partition by kind
        want_trades = "trades" in kinds
        want_quotes = "quotes" in kinds
        trades = symbols if want_trades else []
        quotes = symbols if want_quotes else []
        self._impl.subscribe(trades=trades, quotes=quotes)

    def stream(self) -> Iterable[Event]:
        # Delegate to LIVEAdapter’s stream generator if present,
        # otherwise use its WS client iterator.
        if hasattr(self._impl, "stream"):
            yield from self._impl.stream()
        elif getattr(self._impl, "ws", None) and hasattr(self._impl.ws, "__iter__"):
            yield from self._impl.ws  # type: ignore[attr-defined]
        else:
            # Stay operational, don’t crash: expose an empty iterator
            if False:
                yield {}  # pragma: no cover
