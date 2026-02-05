REM @echo off
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
REM PTI switches
REM ----------------------------

set "PTI_GEN_ID=B"

REM LIVE | REPLAY
set "PTI_FEED_MODE=LIVE"   

if "%PTI_FEED_MODE%"=="LIVE" (
  set "PTI_INTENT_CHANNEL=eval.intent.live"
  set "PTI_BARS_CHANNEL=hub.bars1m.pub.live"
  set "PTI_ACCOUNT_ID=alpaca:paper"

) else (
  set "PTI_INTENT_CHANNEL=eval.intent.replay"
  set "PTI_BARS_CHANNEL=hub.bars1m.pub.replay"
  set "PTI_ACCOUNT_ID=sim:margin"
)

set "PTI_FILTER_STREAM_CHANNEL=eval.1barup_filter_stream"

set "PTI_INTENT_MODE=tee"
REM send | record | tee | off

set "PTI_SYMBOLS_MODE=active_set"     
REM active_set | static
if "%PTI_SYMBOLS_MODE%"=="static" (
    set "PTI_SYMBOLS=SPY,MSFT"
) else (
  set "PTI_ACTIVE_SET_KEY=eval:fts_1barup:active"
  set "PTI_SYMBOLS="  
)

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


echo [RUNNING] %ROOT%evaluator\bots\PTI_1barup.py
echo [ENV] PTI_FEED_MODE=%PTI_FEED_MODE%
echo [ENV] PTI_ACCOUNT_ID=%PTI_ACCOUNT_ID%
echo [ENV] PTI_BARS_CHANNEL=%PTI_BARS_CHANNEL%
echo [ENV] PTI_INTENT_CHANNEL=%PTI_INTENT_CHANNEL%
echo [ENV] PTI_REQUEST_TIER=%PTI_REQUEST_TIER%
echo [ENV] PTI_REQUEST_TIER_NAME=%PTI_REQUEST_TIER_NAME%
echo [ENV] PTI_DEBUG=%PTI_DEBUG%
echo [ENV] PTI_EVENT_LOG_ENABLE=%PTI_EVENT_LOG_ENABLE%
echo [ENV] PTI_EVENT_LOG_PATH=%PTI_EVENT_LOG_PATH%

"%PY%" "%ROOT%\evaluator\bots\PTI_1barup.py"
exit /b %errorlevel%