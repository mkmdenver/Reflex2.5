from shared_mem.registry import registry

def get_conditions():
    results = []
    for symbol, data in registry.items():
        snapshot = data.get("snapshot", {})
        bid = snapshot.get("bid", 0.0)
        ask = snapshot.get("ask", 0.0)
        spread = ask - bid
        volatility = snapshot.get("volatility", 0.0)
        pressure = snapshot.get("tape_pressure", "neutral")
        timestamp = snapshot.get("timestamp", "—")
        session = snapshot.get("session", "unknown")

        results.append({
            "symbol": symbol,
            "spread": spread,
            "volatility": volatility,
            "pressure": pressure,
            "timestamp": timestamp,
            "session": session
        })
    return results