from common.ipc_bus import Bus, new_trace_id, ts_utc_ns, t_mono_ns

async def publish_quote(bus: Bus, sym: str, bid: float, ask: float):
    msg = {"trace_id": new_trace_id(), "ts_utc_ns": ts_utc_ns(), "t_mono_ns": t_mono_ns(), "topic":"quote", "payload": {"symbol": sym, "bid": bid, "ask": ask}}
    await bus.publish(f"reflex:rt:quotes:{sym}", msg)

async def publish_trade(bus: Bus, sym: str, price: float, size: int):
    msg = {"trace_id": new_trace_id(), "ts_utc_ns": ts_utc_ns(), "t_mono_ns": t_mono_ns(), "topic":"trade", "payload": {"symbol": sym, "price": price, "size": size}}
    await bus.publish(f"reflex:rt:trades:{sym}", msg)
