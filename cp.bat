@echo off
setlocal EnableDelayedExpansion

cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"

rem === wire cockpit to your services ===
set DATAHUB_BASE_URL=http://localhost:7000
set EVAL_BASE_URL=http://localhost:7001
set REDIS_URL=redis://127.0.0.1:6379
set REFRESH_FAST_S=1.5
set REFRESH_SLOW_S=5.0

set PYTHONPATH=%cd%

rem If you unzipped the cockpit into 'Cockpit\' (capital C), use this:
echo Starting Cockpit on http://localhost:7010 ...
python -m uvicorn cockpit.main:app --host 0.0.0.0 --port 7010 --reload
