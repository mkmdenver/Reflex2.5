# Evaluator → Trader Order Schema (suggested)

Channel: `orders.new`  (evaluator → trader)

Example:
```json
{
  "ts": 1734123456789012345,          // ns
  "model": "bull_flag_v1",
  "symbol": "AAPL",
  "side": "BUY",                      // BUY | SELL | SELL_SHORT | BUY_TO_COVER
  "order_type": "MKT",                // MKT | LMT | STP | STP_LMT
  "qty": 300,
  "limit_price": null,                // when LMT
  "stop_price": null,                 // when STP/STP_LMT
  "time_in_force": "IOC",             // IOC | DAY | GTC
  "valid_ms": 250,                    // order TTL in ms
  "tags": ["hft","momentum"],
  "risk": {
    "max_slippage_bps": 30,
    "max_spread_bps": 15
  }
}
```

Acks from trader on `orders.acks`:
```json
{"ok":true,"symbol":"AAPL","broker":"alpaca","client_order_id":"...","ts":1734123456790}
```
