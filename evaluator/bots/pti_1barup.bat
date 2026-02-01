@echo off
setlocal EnableExtensions

REM ---------------------------------------------------------------------------
REM KISS launcher for PTI_1barup (channels aligned to Trader Contract)
REM This .bat lives in: <ROOT>\evaluator\bots\
REM ---------------------------------------------------------------------------

set "SCRIPT_DIR=%~dp0"

REM Compute repo root = two levels up from evaluator\bots\
for %%I in ("%SCRIPT_DIR%..\..") do set "ROOT=%%~fI"
set "ROOT=%ROOT%\"

REM BASE = ROOT without trailing backslash (for ROOT.venv fallback)
set "BASE=%ROOT%"
if "%BASE:~-1%"=="\" set "BASE=%BASE:~0,-1%"

REM Load repo env (preferred). PTI python also loads .env/.env.local, but we want
REM the channel vars visible to this process too (for echo/debug & overrides).
if exist "%ROOT%env.bat" (
  call "%ROOT%env.bat"
)

REM Default identity (can be overridden in .env/.env.local or caller shell)
if "%REFLEX_MODE%"=="" set "REFLEX_MODE=LIVE"
if "%REFLEX_INSTANCE_ID%"=="" set "REFLEX_INSTANCE_ID=liveA"
if "%REFLEX_RUN_ID%"=="" set "REFLEX_RUN_ID=run0"

REM Canonical channels (instance-scoped)
if "%PTI_INTENT_CHANNEL%"=="" set "PTI_INTENT_CHANNEL=eval.intent.%REFLEX_INSTANCE_ID%"
if "%MANUAL_INTENT_CHANNEL%"=="" set "MANUAL_INTENT_CHANNEL=manual.intent.%REFLEX_INSTANCE_ID%"
if "%ORDER_CHANNEL%"=="" set "ORDER_CHANNEL=trader.orders.%REFLEX_INSTANCE_ID%"

REM Find venv python (prefer ROOT\.venv, fallback to ROOT.venv)
set "PY=%BASE%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%BASE%.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo [ERROR] Missing venv python. Tried:
  echo   "%BASE%\.venv\Scripts\python.exe"
  echo   "%BASE%.venv\Scripts\python.exe"
  exit /b 1
)

REM ----------------------------
REM PTI switches (edit as needed)
REM ----------------------------
if "%PTI_FEED_MODE%"=="" set "PTI_FEED_MODE=%REFLEX_MODE%"
REM LIVE | REPLAY
if "%PTI_INTENT_MODE%"=="" set "PTI_INTENT_MODE=tee"
REM send | record | tee | off

REM Universe control:
REM set "PTI_SYMBOLS_MODE=static"         REM active_set | static
REM set "PTI_SYMBOLS=SPY,MSFT"            REM only used when static

REM Force PTI into active_set mode
set "PTI_SYMBOLS="
set "PTI_SYMBOLS_MODE=active_set"

REM Point PTI to the 1barup FTS feed
set "PTI_ACTIVE_SET_KEY=eval:fts_1barup:active"
set "PTI_FILTER_STREAM_CHANNEL=eval.1barup_filter_stream"

REM Bars: if caller provided BARS_CHANNEL (from trader_live/trader_replay), reuse it
if "%PTI_BARS_CHANNEL%"=="" if not "%BARS_CHANNEL%"=="" set "PTI_BARS_CHANNEL=%BARS_CHANNEL%"

REM Where intents are recorded (record/tee)
set "PTI_INTENT_RECORD_PATH=%ROOT%evaluator\runs\pti_1barup\intents.jsonl"

REM Account routing (paper for safety)
if "%PTI_ACCOUNT_ID%"=="" set "PTI_ACCOUNT_ID=alpaca:paper"

REM Tier requests (so DataHub will emit bars for WATCH)
set "PTI_REQUEST_TIER=1"
set "PTI_REQUEST_TIER_NAME=WATCH"
set PTI_DEBUG=1
set PTI_EVENT_LOG_ENABLE=1
set PTI_EVENT_LOG_PATH=logs/pti_1barup_events.jsonl
set TRADER_ORDER_LOG_ENABLE=1
set TRADER_ORDER_LOG_PATH=logs/orders_events.jsonl

echo [RUNNING] %ROOT%evaluator\bots\PTI_1barup.py
echo [ENV] REFLEX_MODE=%REFLEX_MODE%
echo [ENV] REFLEX_INSTANCE_ID=%REFLEX_INSTANCE_ID%`
echo [ENV] REFLEX_RUN_ID=%REFLEX_RUN_ID%
echo [ENV] PTI_FEED_MODE=%PTI_FEED_MODE%
echo [ENV] PTI_ACCOUNT_ID=%PTI_ACCOUNT_ID%
echo [ENV] PTI_BARS_CHANNEL=%PTI_BARS_CHANNEL%
echo [ENV] PTI_INTENT_CHANNEL=%PTI_INTENT_CHANNEL%
echo [ENV] MANUAL_INTENT_CHANNEL=%MANUAL_INTENT_CHANNEL%
echo [ENV] ORDER_CHANNEL=%ORDER_CHANNEL%
echo [ENV] PTI_REQUEST_TIER=%PTI_REQUEST_TIER%
echo [ENV] PTI_REQUEST_TIER_NAME=%PTI_REQUEST_TIER_NAME%
echo [ENV] PTI_DEBUG=%PTI_DEBUG%
echo [ENV] PTI_EVENT_LOG_ENABLE=%PTI_EVENT_LOG_ENABLE%
echo [ENV] PTI_EVENT_LOG_PATH=%PTI_EVENT_LOG_PATH%
echo [ENV] TRADER_ORDER_LOG_ENABLE=%TRADER_ORDER_LOG_ENABLE%
echo [ENV] TRADER_ORDER_LOG_PATH=%TRADER_ORDER_LOG_PATH%

"%PY%" "%ROOT%\evaluator\bots\PTI_1barup.py"
exit /b %errorlevel%
