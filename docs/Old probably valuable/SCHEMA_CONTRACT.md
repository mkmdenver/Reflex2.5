# REFLEX2 Schema Contract — **Bedrock**
This document locks the *foundation* schema. New code **must** adhere to this. New tables/columns may be added, but these objects and names stay stable.

## Canonical DDL file (apply this)
`DbManager/sql/schema_timescale_bedrock.sql`

## Extensions
- `timescaledb` must be installed and enabled in the target DB.

## Core facts (Timescale hypertables)
- `tick_data(symbol, timestamp, sip_timestamp, price, size, exchange, conditions[], tape, participant_id, is_trade_through)`
- `quote_data(symbol, timestamp, bid_price, bid_size, ask_price, ask_size, exchange, tape)`
- `minute_bars(symbol, timestamp, open, high, low, close, volume)`
- `daily_bars(symbol, timestamp, open, high, low, close, volume)`

## Aux tables
- `trade_triggers` • `minute_bar_audit` • `fundamental_data`
- `symbol_metadata` • `ingest_sessions` • `evaluator_flags`

## Views (compatibility)
- `ticks(symbol, timestamp, price)` — **INSERT** redirects to `tick_data` (auto synth `sip_timestamp`)
- `quotes(symbol, timestamp, bid, bid_size, ask, ask_size)` — **INSERT** redirects to `quote_data`
- `bars_1m(symbol, bar_time, open, high, low, close, volume)` — alias of `minute_bars` (+ insert redirect)

## Continuous aggregates (from `minute_bars`)
- `agg_5m_bars(bucket, symbol, open, high, low, close, volume)`
- `agg_15m_bars(bucket, symbol, open, high, low, close, volume)`
- `agg_1h_bars(bucket, symbol, open, high, low, close, volume)`
- `agg_1d_bars(bucket, symbol, open, high, low, close, volume)`

## Metrics
- `service_metrics(service, metric, ts, value, labels jsonb, ingest_ts)` (hypertable)
- KPI cagg: `kpi_ingest_1m(bucket, dataset, rows_ingested)`

## Contracts for new code
- **Writes:** prefer direct inserts to `tick_data`, `quote_data`, `minute_bars`. The `ticks/quotes/bars_1m` views exist for legacy callers only.
- **Reads:** for bars use `minute_bars` (1m) or `agg_*` for 5m/15m/1h/1d.
- **Time:** all timestamps are `timestamptz` (UTC). For bars, `timestamp` is the **start of the interval**.
- **Precision:** prices use `NUMERIC(18,6)`; volumes are `BIGINT`.
- **Namespaces:** objects live in `public`. If you must qualify, use `public.<name>`.
- **Don’t rename** any object listed above. Additive changes only (new columns default NULL; new tables; new indexes).

## Preflight
Use `scripts/verify_bedrock.py` before `scripts/run_all` and in CI to catch drift.
