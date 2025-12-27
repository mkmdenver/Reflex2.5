
# Reflex Trader Bundle (RT-1)

This drop contains:
- `trader/` core modules (API, worker, adapters, risk, reconcile, events)
- `db/schema.sql` for Postgres
- `tools/probe_brokers.py` small diagnostics (safe-by-default)
- `scripts/trader.bat`, `scripts/trader-hard.bat`, `scripts/trader-gui.bat`

## Env quickstart
```
set INSTANCE=liveA
set GARNET_URL=redis://127.0.0.1:6379/0
set ALPACA_ACCOUNTS=[{"id":"paper:acct1","label":"Paper-1","class":"margin","pdt_applies":true,"pdt_restricted":false,"allow_short":true,"cash":100000,"buying_power":200000,"equity":100000}]
```
(IB/SCHWAB/WEBULL accounts via IB_ACCOUNTS / SCHWAB_ACCOUNTS / WEBULL_ACCOUNTS)

## Run
- API: `python -m uvicorn trader.app:app --host 0.0.0.0 --port 7002`
- Worker: `python -m trader.broker_worker`
- Or use `scripts/trader.bat` (soft) / `scripts/trader-hard.bat`

## Evaluator → Trader intent (extended)
- see `OrderIn` in `trader/app.py` with fields:
  - `position_intent` (`OPEN|ADD|REDUCE|EXIT|SHORT_OPEN|SHORT_ADD|SHORT_COVER`)
  - `ladder`, `guards` (cooldown enforced today)
  - `strategy_id`, `notes`

## Safety notes
- Emergency controls (`/v1/orders/cancel_all`, `/v1/controls/flatten`) go through **manual** lane.
- Probe tool only places micro orders when `TEST_TRADES_ENABLE=true` AND command args are provided.

## TODO markers
- Adapters: real API clients, throttling, auth flows
- Telemetry: rolling slippage/spread/latency → health keys + events
- Reports: CSV endpoints, idempotent order journal writer
