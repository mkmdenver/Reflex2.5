@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >NUL

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
pushd "%ROOT%"

REM venv python (NO embedded quotes)
set "PY=%ROOT%\.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo [ERROR] venv python not found: %PY%
  popd
  exit /b 1
)

set "REFLEX_INSTANCE_ID=dev"

REM Clean, deterministic runtime
set "PYTHONNOUSERSITE=1"
set "PYTHONPATH=%ROOT%"

REM Load .env if you use env.bat
if exist "%ROOT%\env.bat" (
  call "%ROOT%\env.bat"
)

if not defined DATAHUB_API_PORT set "DATAHUB_API_PORT=7000"

echo [PATH] ROOT=%ROOT%
echo [ENV]  PY=%PY%
echo [ENV]  PYTHONPATH=%PYTHONPATH%
echo [ENV]  DATAHUB_API_PORT=%DATAHUB_API_PORT%
echo [ENV]  REFLEX_PG_DSN=%REFLEX_PG_DSN%
echo [ENV]  REFLEX_INSTANCE_ID=%REFLEX_INSTANCE_ID%

echo.

echo [LAUNCH] DataHub worker
start "DataHub:worker" /min cmd /k ""%PY%" -m datahub.worker"

echo [LAUNCH] DataHub backfill worker
start "DataHub:backfill" /min cmd /k ""%PY%" -m datahub.backfill_worker"

echo [LAUNCH] DataHub API (waitress) on :%DATAHUB_API_PORT%  (datahub.api:flask_app)
start "DataHub:api:%DATAHUB_API_PORT%" /min cmd /k ""%PY%" -m waitress --listen=0.0.0.0:%DATAHUB_API_PORT% datahub.api:flask_app"

popd
exit /b 0