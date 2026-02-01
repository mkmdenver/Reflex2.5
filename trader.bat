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
REM LIVE DEFAULTS (override in .env/.env.local if you want)
REM ===========================================================================
if "%REFLEX_MODE%"=="" set "REFLEX_MODE=LIVE"
if "%REFLEX_INSTANCE_ID%"=="" set "REFLEX_INSTANCE_ID=liveA"



REM Bars feed channel
if "%BARS_CHANNEL%"=="" set "BARS_CHANNEL=hub.bars1m.pub.live"

REM Bot intent channel
if "%BOT_INTENT_CHANNEL%"=="" set "BOT_INTENT_CHANNEL=eval.intent.%REFLEX_INSTANCE_ID%"
if /I "%BOT_INTENT_CHANNEL%"=="eval.intent" set "BOT_INTENT_CHANNEL=eval.intent.%REFLEX_INSTANCE_ID%"

REM Manual intent channel
if "%MANUAL_INTENT_CHANNEL%"=="" set "MANUAL_INTENT_CHANNEL=manual.intent.%REFLEX_INSTANCE_ID%"
if /I "%MANUAL_INTENT_CHANNEL%"=="manual.intent" set "MANUAL_INTENT_CHANNEL=manual.intent.%REFLEX_INSTANCE_ID%"

REM Order channel (internal / observability only)
if "%ORDER_CHANNEL%"=="" set "ORDER_CHANNEL=trader.orders.%REFLEX_INSTANCE_ID%"
if /I "%ORDER_CHANNEL%"=="trader.orders" set "ORDER_CHANNEL=trader.orders.%REFLEX_INSTANCE_ID%"
if /i "%BOT_INTENT_CHANNEL%"=="eval.intent" set "BOT_INTENT_CHANNEL=eval.intent.%REFLEX_INSTANCE_ID%"
if "%MANUAL_INTENT_CHANNEL%"=="" set "MANUAL_INTENT_CHANNEL=manual.intent.%REFLEX_INSTANCE_ID%"
if /i "%MANUAL_INTENT_CHANNEL%"=="manual.intent" set "MANUAL_INTENT_CHANNEL=manual.intent.%REFLEX_INSTANCE_ID%"





REM Back-compat aliases (some tools still use these)
if "%PTI_INTENT_CHANNEL%"=="" set "PTI_INTENT_CHANNEL=%BOT_INTENT_CHANNEL%"
if "%INTENT_CHANNEL%"=="" set "INTENT_CHANNEL=%BOT_INTENT_CHANNEL%"



REM Defaults
if "%TRADER_API_PORT%"=="" set "TRADER_API_PORT=7002"
if "%COCKPIT_PORT%"=="" set "COCKPIT_PORT=7010"

echo.

echo ================== ENV SUMMARY (Trader LIVE) ==================

echo TRADER_EVENT_LOG_ENABLE : %TRADER_EVENT_LOG_ENABLE%
echo TRADER_EVENT_LOG_PATH   : %TRADER_EVENT_LOG_PATH%
echo REFLEX_MODE         : %REFLEX_MODE%
echo REFLEX_INSTANCE_ID  : %REFLEX_INSTANCE_ID%
echo REDIS               : %GARNET_URL%
echo BARS_CHANNEL        : %BARS_CHANNEL%
echo BOT_INTENT_CHANNEL  : %BOT_INTENT_CHANNEL%
echo MANUAL_INTENT_CH    : %MANUAL_INTENT_CHANNEL%
echo TRADER_API_PORT     : %TRADER_API_PORT%
echo COCKPIT_PORT        : %COCKPIT_PORT%
echo ROOT                : %CD%

echo =============================================================

echo.

set TRADER_EVENT_LOG_ENABLE=1
set TRADER_EVENT_LOG_PATH=logs/trader_events.jsonl
set TRADER_ORDER_LOG_ENABLE=1
set TRADER_ORDER_LOG_PATH=logs/orders_events.jsonl
set TRADER_LOG_TO_FILE=1
set TRADER_LOG_PATH=logs/trader.log
set TSLFE_EPS_PRICE=0.01
set TSLFE_TBE_WINDOW=25
set TSLFE_TRIM_TH=1.8
set TSLFE_FLATTEN_TH=2.6
set TSLFE_TRIM_PCT=0.2
set TSLFE_MIN_TRIM_INTERVAL_S=2.0

REM metrics emission
set TRADER_METRICS_EMIT_SECS=1

REM enable stall-based exit
set TRADER_TSLFE_ENABLED=1
set TRADER_TSLFE_EPS_PRICE=0.02
set TRADER_TSLFE_TBE_WINDOW=15
set TRADER_TSLFE_MIN_SAMPLES=12
set TRADER_TSLFE_FLATTEN_TH=1.8
set TRADER_EXIT_MODEL_DEFAULT=tslfe
set TRADER_METRICS_EMIT_SECS=1
set TRADER_TSLFE_PROFIT_EXIT_NORM_TH=2.0


set FLATTEN_CHASE_ENABLE=1
set FLATTEN_ATTEMPTS=8
set FLATTEN_SLEEP_MS=700
set FLATTEN_MAX_SLIPPAGE_PCT=3.0
set FLATTEN_STEP_PCT=0.3

echo [ENV] TRADER_TSLFE_EPS_PRICE : %TRADER_TSLFE_EPS_PRICE%
echo [ENV] TRADER_TSLFE_TBE_WINDOW : %TRADER_TSLFE_TBE_WINDOW%
echo [ENV] TRADER_TSLFE_MIN_SAMPLES : %TRADER_TSLFE_MIN_SAMPLES%
echo [ENV] TRADER_TSLFE_PROFIT_EXIT_NORM_TH : %TRADER_TSLFE_PROFIT_EXIT_NORM_TH%

echo [ENV] FLATTEN_CHASE_ENABLE : %FLATTEN_CHASE_ENABLE%
echo [ENV] FLATTEN_ATTEMPTS : %FLATTEN_ATTEMPTS%
echo [ENV] FLATTEN_SLEEP_MS : %FLATTEN_SLEEP_MS%
echo [ENV] FLATTEN_MAX_SLIPPAGE_PCT : %FLATTEN_MAX_SLIPPAGE_PCT%
echo [ENV] FLATTEN_STEP_PCT : %FLATTEN_STEP_PCT%

echo [ENV] TRADER_EVENT_LOG_ENABLE : %TRADER_EVENT_LOG_ENABLE%
echo [ENV] TRADER_EVENT_LOG_PATH   : %TRADER_EVENT_LOG_PATH%
echo [ENV] TRADER_ORDER_LOG_ENABLE : %TRADER_ORDER_LOG_ENABLE%
echo [ENV] TRADER_ORDER_LOG_PATH   : %TRADER_ORDER_LOG_PATH%
echo [ENV] TRADER_LOG_TO_FILE      : %TRADER_LOG_TO_FILE%
echo [ENV] TRADER_LOG_PATH         : %TRADER_LOG_PATH%
echo [ENV] TSLFE_EPS_PRICE       : %TSLFE_EPS_PRICE%
echo [ENV] TSLFE_TBE_WINDOW      : %TSLFE_TBE_WINDOW%
echo [ENV] TSLFE_TRIM_TH         : %TSLFE_TRIM_TH%
echo [ENV] TSLFE_FLATTEN_TH      : %TSLFE_FLATTEN_TH%
echo [ENV] TSLFE_TRIM_PCT        : %TSLFE_TRIM_PCT%
echo [ENV] TSLFE_MIN_TRIM_INTERVAL_S : %TSLFE_MIN_TRIM_INTERVAL_S%
echo [ENV] TRADER_METRICS_EMIT_SECS : %TRADER_METRICS_EMIT_SECS%
echo [ENV] TRADER_TSLFE_ENABLED : %TRADER_TSLFE_ENABLED%
echo [ENV] TRADER_TSLFE_EPS_PRICE : %TRADER_TSLFE_EPS_PRICE%
echo [ENV] TRADER_TSLFE_TBE_WINDOW : %TRADER_TSLFE_TBE_WINDOW%
echo [ENV] TRADER_TSLFE_MIN_SAMPLES : %TRADER_TSLFE_MIN_SAMPLES%
echo [ENV] TRADER_TSLFE_FLATTEN_TH : %TRADER_TSLFE_FLATTEN_TH%

echo [ENV] TRADER_TSLFE_PROFIT_EXIT_NORM_TH : %TRADER_TSLFE_PROFIT_EXIT_NORM_TH%

if not exist "%ROOT%\.venv\Scripts\python.exe" (

  echo [ERROR] Missing venv python: "%ROOT%\.venv\Scripts\python.exe"

  exit /b 1

)



REM ---------------------------------------------------------------------------

REM Trader API (resident)

REM ---------------------------------------------------------------------------

echo [LAUNCH] Trader API on :%TRADER_API_PORT%

start "Trader:api" /min cmd /k ""%ROOT%\.venv\Scripts\python.exe" -u -m uvicorn trader.app:app --host 127.0.0.1 --port %TRADER_API_PORT%"



REM ---------------------------------------------------------------------------

REM Resident workers (start if present)

REM ---------------------------------------------------------------------------



REM Market-data worker (ticks/quotes/bars listener used by trade mgmt)

if exist "%ROOT%\trader\md_worker.py" (

  echo [LAUNCH] Trader md worker

  start "Trader:md" /min cmd /k ""%ROOT%\.venv\Scripts\python.exe" -u -m trader.md_worker"

) else (

  echo [SKIP] trader\md_worker.py not found

)



REM Broker worker (Redis intents -> POST /v1/intents)

if exist "%ROOT%\trader\broker_worker.py" (

  echo [LAUNCH] Trader broker worker

  start "Trader:broker" /min cmd /k ""%ROOT%\.venv\Scripts\python.exe" -u -m trader.broker_worker"

) else (

  echo [SKIP] trader\broker_worker.py not found

)



REM Intent worker (observer-only by default; safe to run)

if exist "%ROOT%\trader\intent_worker.py" (

  echo [LAUNCH] Trader intent worker (observer)

  start "Trader:intents" /min cmd /k ""%ROOT%\.venv\Scripts\python.exe" -u -m trader.intent_worker"

) else (

  echo [SKIP] trader\intent_worker.py not found

)



REM ---------------------------------------------------------------------------

REM Cockpit (start ONCE)

REM ---------------------------------------------------------------------------

if exist "%ROOT%\brokerview.bat" (

  echo [LAUNCH] BrokerView on :%COCKPIT_PORT%

  start "BrokerView" /min cmd /k ""%ROOT%\brokerview.bat""

  start "" "http://127.0.0.1:%COCKPIT_PORT%/"

) else (

  echo [SKIP] brokerview.bat not found

)



REM ---------------------------------------------------------------------------

REM TradeView (optional)

REM ---------------------------------------------------------------------------

if exist "%ROOT%\traderview.bat" (

  echo [LAUNCH] TradeView

  start "TradeView" /min cmd /k ""%ROOT%\traderview.bat""

) else (

  echo [SKIP] traderview.bat not found

)



endlocal

exit /b 0
