@echo off
setlocal EnableExtensions

REM --- Root folder (this BAT lives in repo root) ---
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

echo [RUNNING] "%~f0"
echo [ROOT] "%ROOT%"

REM --- Load .env + .env.local into THIS process ---
if not exist "%ROOT%\env.bat" (
  echo [ERROR] Missing "%ROOT%\env.bat"
  popd
  exit /b 1
)

call "%ROOT%\env.bat"
if errorlevel 1 (
  echo [ERROR] env.bat returned errorlevel %ERRORLEVEL%
  exit /b %ERRORLEVEL%
)

echo [ENV CHECK] BROKER_DATABASE_URL=%BROKER_DATABASE_URL% REFLEX_BROKER_DSN=%REFLEX_BROKER_DSN%

REM --- Hard fail if DSNs still missing ---
if "%BROKER_DATABASE_URL%"=="" if "%REFLEX_BROKER_DSN%"=="" (
  echo [ERROR] Broker DSN still empty after env load.
  echo         Expected BROKER_DATABASE_URL or REFLEX_BROKER_DSN from .env/.env.local
  exit /b 2
)

REM --- Deterministic python ---
set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] Missing venv python: "%PY%"
  exit /b 3
)

REM --- Defaults ---
if not defined TRADER_API_PORT set "TRADER_API_PORT=7002"
if not defined COCKPIT_PORT set "COCKPIT_PORT=7010"
if not defined REFLEX_INSTANCE_ID set "REFLEX_INSTANCE_ID=liveA"

echo [RUN] Trader API on %TRADER_API_PORT% (instance=%REFLEX_INSTANCE_ID%)
start "Trader:api:%TRADER_API_PORT%" cmd /k ""%PY%" -m uvicorn trader.app:app --host 0.0.0.0 --port %TRADER_API_PORT% --log-level info"

echo [RUN] Broker Worker (instance=%REFLEX_INSTANCE_ID%)
start "Trader:broker_worker" cmd /k ""%PY%" -m trader.broker_worker"

echo [RUN] MD Worker
start "Trader:md_worker" cmd /k ""%PY%" -m trader.md_worker"

if exist "%ROOT%\brokerview.bat" (
  echo [RUN] BrokerView cockpit
  start "BrokerView" cmd /k ""%ROOT%\brokerview.bat""
  start "" "http://127.0.0.1:%COCKPIT_PORT%/"
) else (
  echo [WARN] brokerview.bat not found at "%ROOT%\brokerview.bat"
)

endlocal
exit /b 0
