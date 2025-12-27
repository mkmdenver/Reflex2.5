from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Any, Optional

class SymbolState(Enum):
    COLD = 0
    WATCH = 1
    WARM = 2
    HOT = 3

@dataclass
class SymbolFlags:
    no_trade: bool = False
    halted: bool = False
    restricted: bool = False
    custom: Dict[str, Any] = field(default_factory=dict)

@dataclass
class SymbolMeta:
    symbol: str
    state: SymbolState = SymbolState.COLD
    flags: SymbolFlags = field(default_factory=SymbolFlags)
    last_price: Optional[float] = None
    last_bid: Optional[float] = None
    last_ask: Optional[float] = None
    fundamentals: Dict[str, Any] = field(default_factory=dict)
    filters: Dict[str, Any] = field(default_factory=dict)
