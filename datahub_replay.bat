@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >NUL

REM ------------------------------------------------------------------
REM DataHub REPLAY launcher
REM   - Uses common .venv and common .env (via Python config)
REM   - Starts REPLAY worker and REPLAY HTTP API
REM   - Designed to run side-by-side with LIVE (different ports/instance)
REM ------------------------------------------------------------------

REM Resolve repo root (this .bat lives in the repo root)
set "ROOT=%~dp0"
pushd "%ROOT%"

REM --- Activate virtualenv ---
if exist "%ROOT%\.venv\Scripts\activate.bat" (
  call "%ROOT%\.venv\Scripts\activate.bat"
) else (
  echo [ERROR] venv not found at %ROOT%\.venv
  popd
  exit /b 1
)

set "PY=%ROOT%\.venv\Scripts\python.exe"
set "PYTHONPATH=%ROOT%"

REM ------------------------------------------------------------------
REM REPLAY defaults
REM   - Different ports so LIVE + REPLAY can coexist
REM   - Instance defaults to replayA
REM   - Any of these can be overridden before calling the .bat
REM ------------------------------------------------------------------
if not defined DATAHUB_INTERNAL_PORT set DATAHUB_INTERNAL_PORT=7170
if not defined DATAHUB_API_PORT      set DATAHUB_API_PORT=7100

REM Force REPLAY mode for this launcher
set "REFLEX__HUB_MODE=REPLAY"

REM Give REPLAY its own default instance name
if not defined REFLEX__INSTANCE_ID set REFLEX__INSTANCE_ID=replayA

REM (Future) Time controls for replay could live here, e.g.:
REM   set "REFLEX__REPLAY_DATE=2025-11-20"
REM   set "REFLEX__REPLAY_SPEED=1.0"
REM We'll wire these once the Python side expects them.

echo [PATH] ROOT=%ROOT%"
echo [ENV]  PY=%PY%
echo [ENV]  PYTHONPATH=%PYTHONPATH%
echo [ENV]  REFLEX__HUB_MODE=%REFLEX__HUB_MODE%
echo [ENV]  REFLEX__INSTANCE_ID=%REFLEX__INSTANCE_ID%
echo [ENV]  DATAHUB_INTERNAL_PORT=%DATAHUB_INTERNAL_PORT%
echo [ENV]  DATAHUB_API_PORT=%DATAHUB_API_PORT%
echo.

REM ------------------------------------------------------------------
REM Launch DataHub REPLAY worker
REM   - Python side will look at REFLEX__HUB_MODE=REPLAY and choose
REM     the replay adapter / clock once we finish that wiring.
REM ------------------------------------------------------------------
echo [LAUNCH] DataHub REPLAY worker (mode=%REFLEX__HUB_MODE%)
start "DataHub:REPLAY:worker" cmd /k "%PY%" -m datahub.worker

REM ------------------------------------------------------------------
REM REPLAY typically doesn’t need the live backfill worker, so we skip it
REM   - If you decide replay wants its own backfill process, we can add
REM     a dedicated datahub.replay_backfill_worker later.
REM ------------------------------------------------------------------

REM ------------------------------------------------------------------
REM Launch DataHub REPLAY HTTP API (tiers / health / controls)
REM ------------------------------------------------------------------
where waitress-serve >NUL 2>&1
if not errorlevel 1 (
  echo [LAUNCH] waitress-serve on :%DATAHUB_API_PORT%  (datahub.api:flask_app, REPLAY)
  start "DataHub:REPLAY:api:%DATAHUB_API_PORT%" cmd /k waitress-serve --listen=0.0.0.0:%DATAHUB_API_PORT% datahub.api:flask_app
  popd
  exit /b 0
)

echo [INFO] waitress-serve not found; using Flask CLI (REPLAY).
set "FLASK_APP=datahub.api:flask_app"
start "DataHub:REPLAY:api:%DATAHUB_API_PORT%" cmd /k "%PY%" -m flask run --host 0.0.0.0 --port %DATAHUB_API_PORT%

popd
exit /b 0
