# Reflex OpsView (stubs)

A lightweight Vite/React UI that stubs out three cockpit views:

- **ModelView** (replacement for EvalView/ModelView)
- **Portfolio/RiskView**
- **EngineerView**

## Known hooks (already live)

This UI calls existing endpoints:

### Trader API (FastAPI)
- `GET /v1/health`
- `GET /v1/portfolio/overview`
- `GET /v1/intents?limit=...`
- `GET /v1/trades?status=...`
- `GET /v1/events` (SSE)

See `trader/app.py` v2.1.0.

### DataHub API (Flask)
- `GET /v1/history/bars1m?symbol=...&limit=...`

See `datahub/api.py`.

## Dev run

1. From this folder:

```bash
npm install
npm run dev
```

2. By default Vite proxies:
- `/trader` -> `http://127.0.0.1:7002`
- `/datahub` -> `http://127.0.0.1:7001`

If your ports differ, adjust `vite.config.ts`.

## Notes

These are **stubs** intended as a working GUI for discussion. The RiskView “allocations” are **client-side only** until you add real Risk service endpoints.
