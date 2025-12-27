# --- model_tracker.py ---
# Logs model progression stats per symbol for diagnostics and replay analysis

import time
import csv
import os
from datetime import datetime
from shared_mem.registry import registry

LOG_PATH = os.path.abspath("logs/model_progression.csv")

def start_model_tracker():
    print("[📈] Model tracker started...")
    interval = 2.0  # Log every 2 seconds

    fields = [
        "timestamp", "symbol", "state", "entry_triggered", "exit_triggered",
        "add_count", "gain_points", "drawdown_points", "momentum", "rsi", "atr", "vwap"
    ]

    write_header = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if write_header:
            writer.writeheader()

    while True:
        with open(LOG_PATH, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            timestamp = datetime.utcnow().isoformat()
            for symbol, data in registry.items():
                snapshot = data.get("snapshot", {})
                flags = data.get("flags", {})
                row = {
                    "timestamp": timestamp,
                    "symbol": symbol,
                    "state": data.get("state", ""),
                    "entry_triggered": flags.get("entry_triggered", False),
                    "exit_triggered": flags.get("exit_triggered", False),
                    "add_count": flags.get("add_count", 0),
                    "gain_points": snapshot.get("gain_points", ""),
                    "drawdown_points": snapshot.get("drawdown_points", ""),
                    "momentum": snapshot.get("momentum", ""),
                    "rsi": snapshot.get("rsi", ""),
                    "atr": snapshot.get("atr", ""),
                    "vwap": snapshot.get("vwap", "")
                }
                writer.writerow(row)
        time.sleep(interval)