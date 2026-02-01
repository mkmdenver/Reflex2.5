@echo off
setlocal EnableExtensions

REM --- This BAT lives in repo root ---
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

echo [RUNNING] "%~f0"
echo [ROOT] "%ROOT%"

cd /d "%ROOT%"

REM --- Load .env + .env.local into THIS process ---
if not exist "%ROOT%\env.bat" (
  echo [ERROR] Missing "%ROOT%\env.bat"
  exit /b 1
)
call "%ROOT%\env.bat"
if errorlevel 1 exit /b 1

REM ===========================================================================
REM REPLAY DEFAULTS (override in .env/.env.local if you want)
REM ===========================================================================
set "REFLEX_MODE=REPLAY"
if "%REFLEX_INSTANCE_ID%"=="" set "REFLEX_INSTANCE_ID=replayA"

REM Bars feed channel
if "%BARS_CHANNEL%"=="" set "BARS_CHANNEL=hub.bars1m.pub.replay"

REM Canonical intent channels (instance-scoped)
if "%BOT_INTENT_CHANNEL%"=="" set "BOT_INTENT_CHANNEL=eval.intent.%REFLEX_INSTANCE_ID%"
if "%MANUAL_INTENT_CHANNEL%"=="" set "MANUAL_INTENT_CHANNEL=manual.intent.%REFLEX_INSTANCE_ID%"

REM Back-compat aliases (some tools still use these)
if "%PTI_INTENT_CHANNEL%"=="" set "PTI_INTENT_CHANNEL=%BOT_INTENT_CHANNEL%"
if "%INTENT_CHANNEL%"=="" set "INTENT_CHANNEL=%BOT_INTENT_CHANNEL%"

REM Safety: prefer SIM-only in replay unless you explicitly override elsewhere
if "%TRADER_BROKER_MODE%"=="" set "TRADER_BROKER_MODE=SIM"
if "%ALPACA_ENABLED%"=="" set "ALPACA_ENABLED=0"

REM Defaults (use different ports to avoid stomping LIVE)
if "%TRADER_API_PORT%"=="" set "TRADER_API_PORT=7012"
if "%COCKPIT_PORT%"=="" set "COCKPIT_PORT=7020"

echo.
echo ================== ENV SUMMARY (Trader REPLAY) =================
echo REFLEX_MODE         : %REFLEX_MODE%
echo REFLEX_INSTANCE_ID  : %REFLEX_INSTANCE_ID%
echo REDIS               : %GARNET_URL%
echo BARS_CHANNEL        : %BARS_CHANNEL%
echo BOT_INTENT_CHANNEL  : %BOT_INTENT_CHANNEL%
echo MANUAL_INTENT_CH    : %MANUAL_INTENT_CHANNEL%
echo TRADER_BROKER_MODE  : %TRADER_BROKER_MODE%
echo ALPACA_ENABLED      : %ALPACA_ENABLED%
echo TRADER_API_PORT     : %TRADER_API_PORT%
echo COCKPIT_PORT        : %COCKPIT_PORT%
echo ROOT                : %CD%
echo ==============================================================
echo.

if not exist "%ROOT%\.venv\Scripts\python.exe" (
  echo [ERROR] Missing venv python: "%ROOT%\.venv\Scripts\python.exe"
  exit /b 1
)

REM ---------------------------------------------------------------------------
REM Trader API (resident)
REM ---------------------------------------------------------------------------
echo [LAUNCH] Trader API on :%TRADER_API_PORT%
start "Trader:api(replay)" cmd /k ""%ROOT%\.venv\Scripts\python.exe" -u -m uvicorn trader.app:app --host 127.0.0.1 --port %TRADER_API_PORT%"

REM ---------------------------------------------------------------------------
REM Resident workers
REM ---------------------------------------------------------------------------

if exist "%ROOT%\trader\md_worker.py" (
  echo [LAUNCH] Trader md worker (replay)
  start "Trader:md(replay)" cmd /k ""%ROOT%\.venv\Scripts\python.exe" -u -m trader.md_worker"
) else (
  echo [SKIP] trader\md_worker.py not found
)

if exist "%ROOT%\trader\broker_worker.py" (
  echo [LAUNCH] Trader broker worker (replay)
  start "Trader:broker(replay)" cmd /k ""%ROOT%\.venv\Scripts\python.exe" -u -m trader.broker_worker"
) else (
  echo [SKIP] trader\broker_worker.py not found
)

if exist "%ROOT%\trader\intent_worker.py" (
  echo [LAUNCH] Trader intent worker (observer, replay)
  start "Trader:intents(replay)" cmd /k ""%ROOT%\.venv\Scripts\python.exe" -u -m trader.intent_worker"
) else (
  echo [SKIP] trader\intent_worker.py not found
)

REM ---------------------------------------------------------------------------
REM Views (optional in replay; start if present)
REM ---------------------------------------------------------------------------
if exist "%ROOT%\brokerview.bat" (
  echo [LAUNCH] BrokerView on :%COCKPIT_PORT% (replay)
  start "BrokerView(replay)" cmd /k ""%ROOT%\brokerview.bat""
  start "" "http://127.0.0.1:%COCKPIT_PORT%/"
) else (
  echo [SKIP] brokerview.bat not found
)

if exist "%ROOT%\traderview.bat" (
  echo [LAUNCH] TradeView (replay)
  start "TradeView(replay)" cmd /k ""%ROOT%\traderview.bat""
) else (
  echo [SKIP] traderview.bat not found
)

endlocal
exit /b 0
