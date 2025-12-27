# Reflex — Database Schema (TimescaleDB)

> Timescale extension required. Hypertables for time‑series; continuous aggregates for multi‑resolution analysis.

## Core Tables (hypertables)
- `tick_data(symbol, timestamp, sip_timestamp, price, size, exchange, conditions[], tape, participant_id, is_trade_through)`  
  - Unique `(symbol, timestamp, sip_timestamp)`; index `idx_tick_conflict`.
- `quote_data(symbol, timestamp, bid_price, bid_size, ask_price, ask_size, exchange, tape)` (PK `(symbol, timestamp)`)
- `minute_bars(symbol, timestamp, open, high, low, close, volume)` (PK `(symbol, timestamp)`)
- `daily_bars(symbol, timestamp, open, high, low, close, volume)` (PK `(symbol, timestamp)`)

## Analytics (continuous aggregates)
- `agg_5m_bars`, `agg_15m_bars`, `agg_1h_bars`, `agg_1d_bars` over `minute_bars`

## Fundamentals & Metadata
- `fundamental_data(symbol PK, company, sector, industry, country, exchange, market_cap, pe_ratio, shares_float, float_percent, insider_transactions, short_float, average_true_range, last_updated)`
  - Indices on `symbol, sector, industry, country, exchange, market_cap, pe_ratio, shares_float, float_percent`.

- `symbol_metadata(symbol PK, mode, filters[], last_updated)`
- `ingest_sessions(session_id UUID PK, start_time, source, notes)`
- `evaluator_flags(id PK, symbol, timestamp, flag_type, confidence, metadata)`
- `minute_bar_audit(id PK, symbol, timestamp, ingested_volume, expected_volume, integrity_passed)`
- `trade_triggers(id PK, symbol, trigger_type, timestamp, metadata)`

## Views
- `symbol_profile_view` joining fundamentals + latest flag metadata for operator visibility.

---
*Structured from [Reflex-schema.md](https://rockymountaintechnet-my.sharepoint.com/personal/mike_malone_rockymountaintech_net/Documents/Forms/DispForm.aspx?ID=674&web=1&EntityRepresentationId=3a3dfbca-0175-4503-b5dc-e2f265d62149) SQL statements.* [11](https://rockymountaintechnet-my.sharepoint.com/personal/mike_malone_rockymountaintech_net/Documents/Reflex-compromised/Reflex-schema.md)
