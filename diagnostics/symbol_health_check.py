# diagnostics/symbol_health_check.py
from shared_mem.registry import registry

def symbol_health_check(symbol):
    from shared_mem.registry import registry
    buf = registry[symbol]['buffer']
    snapshot_ready = registry[symbol]['snapshot'] is not None
    return {
        "buffer_len": len(buf.get_short()),
        "snapshot_ready": snapshot_ready,
        "reflexive_depth": len(buf.get_short()),
        "contextual_depth": len(buf.get_long()),
        "state": registry[symbol]['state']
    }

def start_diagnostics():
    from time import sleep
    while True:
        for symbol in list(registry.keys())[:10]:
            status = symbol_health_check(symbol)
            print(f"[🔍] {symbol}: {status}")
        sleep(60)