# --- model_logger.py ---
# Logs model decisions for diagnostics and replay analysis

import csv
import os
from datetime import datetime
from shared_mem.registry import registry  # ✅ Required import

LOG_PATH = os.path.abspath("logs/model_decisions.csv")

def log_model_decision(symbol, action, model, snapshot, flags):
    fields = [
        "timestamp", "symbol", "action", "model_name", "state",
        "entry_triggered", "exit_triggered", "add_count",
        "momentum", "rsi", "atr", "vwap", "gain", "drawdown"
    ]

    row = {
        "timestamp": datetime.utcnow().isoformat(),
        "symbol": symbol,
        "action": action,
        "model_name": model.get("model_name", "default"),
        "state": registry[symbol].get("state", ""),
        "entry_triggered": flags.get("entry_triggered", False),
        "exit_triggered": flags.get("exit_triggered", False),
        "add_count": flags.get("add_count", 0),
        "momentum": snapshot.get("momentum", ""),
        "rsi": snapshot.get("rsi", ""),
        "atr": snapshot.get("atr", ""),
        "vwap": snapshot.get("vwap", ""),
        "gain": snapshot.get("gain_points", ""),
        "drawdown": snapshot.get("drawdown_points", "")
    }

    write_header = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if write_header:
            writer.writeheader()
        writer.writerow(row)