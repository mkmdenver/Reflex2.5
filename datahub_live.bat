@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >NUL

REM ------------------------------------------------------------------
REM DataHub LIVE launcher
REM   - Uses common .venv and common .env (via Python config)
REM   - Starts LIVE worker, LIVE backfill worker, and HTTP API
REM ------------------------------------------------------------------

REM Resolve repo root (this .bat lives in the repo root)
set "ROOT=%~dp0"
pushd "%ROOT%"

REM --- Pick Python from .venv if present ---
if exist "%ROOT%\.venv\Scripts\python.exe" (
  set "PY=%ROOT%\.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

REM --- Core environment for LIVE hub ---
if not defined PYTHONPATH set "PYTHONPATH=%ROOT%"
if not defined REFLEX__HUB_MODE set "REFLEX__HUB_MODE=LIVE"
if not defined REFLEX__INSTANCE_ID set "REFLEX__INSTANCE_ID=liveA"
if not defined DATAHUB_INTERNAL_PORT set "DATAHUB_INTERNAL_PORT=7070"
if not defined DATAHUB_API_PORT set "DATAHUB_API_PORT=7000"

echo [ENV]  PYTHONPATH=%PYTHONPATH%
echo [ENV]  REFLEX__HUB_MODE=%REFLEX__HUB_MODE%
echo [ENV]  REFLEX__INSTANCE_ID=%REFLEX__INSTANCE_ID%
echo [ENV]  DATAHUB_INTERNAL_PORT=%DATAHUB_INTERNAL_PORT%
echo [ENV]  DATAHUB_API_PORT=%DATAHUB_API_PORT%

REM ------------------------------------------------------------------
REM LIVE worker (main hub engine: subs, tiers, ingest, etc.)
REM ------------------------------------------------------------------
echo.
echo [LAUNCH] DataHub LIVE worker (mode=%REFLEX__HUB_MODE%)
start "DataHub:LIVE:worker:%REFLEX__INSTANCE_ID%" cmd /k "%PY%" -m datahub.worker

REM ------------------------------------------------------------------
REM LIVE backfill worker (today-only minute/tick hydration)
REM ------------------------------------------------------------------
echo [LAUNCH] DataHub LIVE backfill worker
start "DataHub:LIVE:backfill:%REFLEX__INSTANCE_ID%" cmd /k "%PY%" -m datahub.backfill_worker

REM ------------------------------------------------------------------
REM HTTP API (internal + public API surface)
REM ------------------------------------------------------------------
where waitress-serve >NUL 2>&1
if not errorlevel 1 (
  echo [LAUNCH] waitress-serve on :%DATAHUB_API_PORT%  (datahub.api:flask_app, LIVE)
  start "DataHub:LIVE:api:%DATAHUB_API_PORT%" cmd /k waitress-serve --listen=0.0.0.0:%DATAHUB_API_PORT% datahub.api:flask_app
  popd
  endlocal
  exit /b 0
)

echo [INFO] waitress-serve not found; using Flask CLI (LIVE).
set "FLASK_APP=datahub.api:flask_app"
echo [LAUNCH] Flask dev server on :%DATAHUB_API_PORT%  (datahub.api:flask_app, LIVE)
start "DataHub:LIVE:api:%DATAHUB_API_PORT%" cmd /k "%PY%" -m flask run --host 0.0.0.0 --port %DATAHUB_API_PORT%

popd
endlocal
exit /b 0
