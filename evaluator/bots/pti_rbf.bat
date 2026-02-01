@echo off
setlocal EnableExtensions

REM ---------------------------------------------------------------------------
REM KISS launcher for PTI_rbf (channels aligned to Trader Contract)
REM This .bat lives in: <ROOT>\evaluator\bots\
REM ---------------------------------------------------------------------------

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..") do set "ROOT=%%~fI"
set "ROOT=%ROOT%\"
set "BASE=%ROOT%"
if "%BASE:~-1%"=="\" set "BASE=%BASE:~0,-1%"

if exist "%ROOT%env.bat" (
  call "%ROOT%env.bat"
)

if "%REFLEX_MODE%"=="" set "REFLEX_MODE=LIVE"
if "%REFLEX_INSTANCE_ID%"=="" set "REFLEX_INSTANCE_ID=liveA"
if "%REFLEX_RUN_ID%"=="" set "REFLEX_RUN_ID=run0"

REM Canonical channels (instance-scoped)
if "%PTI_INTENT_CHANNEL%"=="" set "PTI_INTENT_CHANNEL=eval.intent.%REFLEX_INSTANCE_ID%"
if "%MANUAL_INTENT_CHANNEL%"=="" set "MANUAL_INTENT_CHANNEL=manual.intent.%REFLEX_INSTANCE_ID%"
if "%ORDER_CHANNEL%"=="" set "ORDER_CHANNEL=trader.orders.%REFLEX_INSTANCE_ID%"

set "PY=%BASE%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERR] Repo venv not found: "%PY%"
  exit /b 1
)

pushd "%BASE%"

REM Feed mode follows REFLEX_MODE unless overridden
if "%PTI_FEED_MODE%"=="" set "PTI_FEED_MODE=%REFLEX_MODE%"

REM Bars: reuse BARS_CHANNEL if present
if "%PTI_BARS_CHANNEL%"=="" if not "%BARS_CHANNEL%"=="" set "PTI_BARS_CHANNEL=%BARS_CHANNEL%"

REM Account routing (paper for safety)
if "%PTI_ACCOUNT_ID%"=="" set "PTI_ACCOUNT_ID=alpaca:paper"

REM Tier requests
set "PTI_REQUEST_TIER=1"
set "PTI_REQUEST_TIER_NAME=WATCH"

set "PTI_DEBUG=1"
set "PTI_EVENT_LOG_ENABLE=1"
set "PTI_EVENT_LOG_PATH=logs/pti_rbf_events.jsonl"
set "PTI_DEBUG_THROTTLE_SEC=10"

echo [ENV] REFLEX_MODE=%REFLEX_MODE%
echo [ENV] REFLEX_INSTANCE_ID=%REFLEX_INSTANCE_ID%
echo [ENV] REFLEX_RUN_ID=%REFLEX_RUN_ID%
echo [ENV] PTI_FEED_MODE=%PTI_FEED_MODE%
echo [ENV] PTI_ACCOUNT_ID=%PTI_ACCOUNT_ID%
echo [ENV] PTI_BARS_CHANNEL=%PTI_BARS_CHANNEL%
echo [ENV] PTI_INTENT_CHANNEL=%PTI_INTENT_CHANNEL%
echo [ENV] MANUAL_INTENT_CHANNEL=%MANUAL_INTENT_CHANNEL%
echo [ENV] ORDER_CHANNEL=%ORDER_CHANNEL%
echo [ENV] PTI_DEBUG=%PTI_DEBUG%
echo [ENV] PTI_EVENT_LOG_ENABLE=%PTI_EVENT_LOG_ENABLE%
echo [ENV] PTI_EVENT_LOG_PATH=%PTI_EVENT_LOG_PATH%
echo [ENV] PTI_DEBUG_THROTTLE_SEC=%PTI_DEBUG_THROTTLE_SEC%

echo [RUN] %PY% evaluator\bots\PTI_rbf.py
"%PY%" "%ROOT%\evaluator\bots\PTI_rbf.py"
set RC=%ERRORLEVEL%

popd
exit /b %RC%
