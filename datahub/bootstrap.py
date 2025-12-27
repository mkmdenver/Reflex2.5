from __future__ import annotations
from .registry import Registry
from .models import SymbolFlags, SymbolState
from . import dbio

def hydrate_from_db(reg: Registry) -> None:
    symbols = dbio.get_all_symbols()
    flags_raw = dbio.get_symbol_flags()
    states_raw = dbio.get_initial_states()

    flags_map = { s: SymbolFlags(**vals) for s, vals in flags_raw.items() }
    state_map = { s: getattr(SymbolState, (st or 'COLD').upper(), SymbolState.COLD) for s, st in states_raw.items() }

    reg.hydrate_symbols(symbols, state_map, flags_map)
