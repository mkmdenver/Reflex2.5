# BrokerView (v0.5.1)

- Reads **ROOT** `.env` (two levels up).
- Uses `BROKER_DATABASE_URL` for journaling (fallback: `REFLEX_BROKER_DSN`, `REFLEX_PG_DSN`).
- API listens on `BROKERVIEW_PORT` (default **7010**).
- Trader remains on `TRADER_API_PORT` (default **7002**) — only for reference; UI speaks to BrokerView.

## Start
From **project root**:
```
brokerview
```
This wrapper calls `cockpit\brokerview\brokerview.bat`, creates a venv if needed, installs from root `requirements.txt`, and starts uvicorn.
