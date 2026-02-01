@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================================
REM FTS_1BarUp runner
REM - Lives in: <root>\evaluator\bots\fts_1barup.bat
REM - Runs: evaluator\bots\FTS_1barup.py
REM - Two scenarios:
REM     A) STATIC  : you provide a CSV list (FTS_1BAR_SYMBOLS)
REM     B) DBSCAN  : scans Postgres for shares_float + latest daily close < $20
REM - This bot owns a dedicated membership feed (active set + stream) for PTI_1barup
REM     active set key      : FTS_1BAR_ACTIVE_KEY
REM     filter stream chan  : FTS_1BAR_FILTER_STREAM_CHANNEL
REM ============================================================================

REM --- repo root: this bat lives in <root>\evaluator\bots ---
set "ROOT=%~dp0..\.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"

echo [PATH] ROOT=%ROOT%

REM --- set identity (safe defaults) ---
if "%REFLEX_MODE%"=="" set "REFLEX_MODE=LIVE"
if "%REFLEX_INSTANCE_ID%"=="" set "REFLEX_INSTANCE_ID=liveA"
if "%REFLEX_RUN_ID%"=="" set "REFLEX_RUN_ID=run0"

echo [ENV] REFLEX_MODE=%REFLEX_MODE%
echo [ENV] REFLEX_INSTANCE_ID=%REFLEX_INSTANCE_ID%
echo [ENV] REFLEX_RUN_ID=%REFLEX_RUN_ID%

REM ============================================================================
REM CONFIG: Scenario selection
REM   Set FTS_1BAR_MODE=static  or  dbscan
REM ============================================================================
if "%FTS_1BAR_MODE%"=="" set "FTS_1BAR_MODE=static"

REM ============================================================================
REM CONFIG: Dedicated membership feed (PTI_1barup should point at these)
REM ============================================================================
if "%FTS_1BAR_ACTIVE_KEY%"=="" set "FTS_1BAR_ACTIVE_KEY=eval:fts_1barup:active"
if "%FTS_1BAR_FILTER_STREAM_CHANNEL%"=="" set "FTS_1BAR_FILTER_STREAM_CHANNEL=eval.1barup_filter_stream"

echo [ENV] FTS_1BAR_MODE=%FTS_1BAR_MODE%
echo [ENV] FTS_1BAR_ACTIVE_KEY=%FTS_1BAR_ACTIVE_KEY%
echo [ENV] FTS_1BAR_FILTER_STREAM_CHANNEL=%FTS_1BAR_FILTER_STREAM_CHANNEL%

REM ============================================================================
REM CONFIG: Tier control (optional but recommended)
REM   WATCH tier prompts DataHub to pump 1m bars for these symbols.
REM ============================================================================
if "%FTS_1BAR_RAISE_ENABLE%"=="" set "FTS_1BAR_RAISE_ENABLE=1"
if "%FTS_1BAR_RAISE_TIER%"=="" set "FTS_1BAR_RAISE_TIER=WATCH"

echo [ENV] FTS_1BAR_RAISE_ENABLE=%FTS_1BAR_RAISE_ENABLE%
echo [ENV] FTS_1BAR_RAISE_TIER=%FTS_1BAR_RAISE_TIER%

REM ============================================================================
REM SCENARIO A: STATIC LIST
REM   Provide a comma-separated list. Example:
REM     set "FTS_1BAR_SYMBOLS=SPY,IWM,BNAI"
REM ============================================================================
if "%FTS_1BAR_SYMBOLS%"=="" set "FTS_1BAR_SYMBOLS=SPY,TSLA,MSFT,AGIG,SUGP,RGNT,GNPX,BYSI,MLGO,LESL,ARMP,SPHL,CJMB,MLEC,AUID,PTHL,AHMA,CGTL,IBRX,CRGO"

REM ============================================================================
REM SCENARIO B: DBSCAN (Postgres)
REM   Uses:
REM     - latest daily_bars.close < FTS_1BAR_PRICE_MAX
REM     - fundamental_data.shares_float between [MIN, MAX] (or NULL if allowed)
REM   Requires REFLEX_PG_DSN in .env / .env.local (or set here).
REM ============================================================================
if "%FTS_1BAR_PRICE_MAX%"=="" set "FTS_1BAR_PRICE_MAX=20"
if "%FTS_1BAR_FLOAT_ALLOW_NULL%"=="" set "FTS_1BAR_FLOAT_ALLOW_NULL=1"
if "%FTS_1BAR_FLOAT_MIN%"=="" set "FTS_1BAR_FLOAT_MIN=1000000"
if "%FTS_1BAR_FLOAT_MAX%"=="" set "FTS_1BAR_FLOAT_MAX=20000000"
if "%FTS_1BAR_REFRESH_SEC%"=="" set "FTS_1BAR_REFRESH_SEC=300"

echo [ENV] FTS_1BAR_SYMBOLS=%FTS_1BAR_SYMBOLS%
echo [ENV] FTS_1BAR_PRICE_MAX=%FTS_1BAR_PRICE_MAX%
echo [ENV] FTS_1BAR_FLOAT_ALLOW_NULL=%FTS_1BAR_FLOAT_ALLOW_NULL%
echo [ENV] FTS_1BAR_FLOAT_MIN=%FTS_1BAR_FLOAT_MIN%
echo [ENV] FTS_1BAR_FLOAT_MAX=%FTS_1BAR_FLOAT_MAX%
echo [ENV] FTS_1BAR_REFRESH_SEC=%FTS_1BAR_REFRESH_SEC%

REM --- always run the repo venv python, not a random one ---
set "PY=%ROOT%\.venv\Scripts\python.exe"
echo [RUN] %PY% evaluator\bots\FTS_1barup.py

pushd "%ROOT%"
"%PY%" "evaluator\bots\FTS_1barup.py"
popd

endlocal
