
Reflex 2.2 Full Merge
=====================

This directory is your original upload with the 2.2 changes applied in-place.

Generated: 2025-10-21T18:31:03.525512Z
Key additions:
- DB-driven partitioning and tier-based subscriptions (COLD→WATCH→WARM→HOT)
- LIVE/REPLAY adapter seam (Polygon WS / Parquet replay)
- Parquet drop-copy writers for trades and quotes
- Structured JSON logging and Prometheus /metrics endpoint
- Instance-scoped .env templates and launcher script

New/updated notable paths:
- common/config.py, common/config.sys
- common/logging.py, common/metrics.py, common/market_adapter.py
- adapters/live_adapter.py, adapters/replay_adapter.py
- io/parquet_writer.py
- datahub/worker.py, datahub/api.py
- evaluator/service.py
- trader/alpaca_adapter.py
- scripts/Start-ReflexPair.ps1
- .env.liveA, .env.replayGME

Drop this entire folder in place of your current repo directory (or clone a new branch and copy it in).

