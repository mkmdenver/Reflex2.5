@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
pushd "%ROOT%"

echo [RUNNING] %~f0

rem --- Load root .env (env.bat REQUIRES a file path arg) ---
call "%ROOT%env.bat" "%ROOT%.env"
if errorlevel 1 (
  echo [ERROR] env.bat failed. Expected: call env.bat path\to\.env
  popd
  exit /b 1
)

rem --- Deterministic python: NO activate, NO PATH games ---
set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] Missing venv python: "%PY%"
  popd
  exit /b 1
)

rem --- Defaults ---
if not defined TRADER_API_PORT set "TRADER_API_PORT=7002"
if not defined COCKPIT_PORT set "COCKPIT_PORT=7010"
if not defined REFLEX_INSTANCE_ID set "REFLEX_INSTANCE_ID=liveA"

echo [RUN] Trader API on %TRADER_API_PORT% (instance=%REFLEX_INSTANCE_ID%)
start "Trader:api:%TRADER_API_PORT%" cmd /k ""%PY%" -m uvicorn trader.app:app --host 0.0.0.0 --port %TRADER_API_PORT%"

echo [RUN] Broker Worker (instance=%REFLEX_INSTANCE_ID%)
start "Trader:broker_worker" cmd /k ""%PY%" -m trader.broker_worker --instance %REFLEX_INSTANCE_ID%"

echo [RUN] MD Worker
start "Trader:md_worker" cmd /k ""%PY%" -m trader.md_worker"

if exist "%ROOT%brokerview.bat" (
  echo [RUN] BrokerView cockpit on http://127.0.0.1:%COCKPIT_PORT%/
  start "BrokerView" cmd /k "%ROOT%brokerview.bat"
  start "" "http://127.0.0.1:%COCKPIT_PORT%/"
) else (
  echo [WARN] brokerview.bat not found at "%ROOT%brokerview.bat"
)

popd
endlocal
exit /b 0
