@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM --- repo root: this bat lives in <root>\evaluator\bots ---
set "ROOT=%~dp0..\.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"

echo [PATH] ROOT=%ROOT%

REM --- set identity ---
if "%REFLEX_MODE%"=="" set "REFLEX_MODE=LIVE"
if "%REFLEX_INSTANCE_ID%"=="" set "REFLEX_INSTANCE_ID=live"
if "%REFLEX_RUN_ID%"=="" set "REFLEX_RUN_ID=run0"

echo [ENV] REFLEX_MODE=%REFLEX_MODE%
echo [ENV] REFLEX_INSTANCE_ID=%REFLEX_INSTANCE_ID%
echo [ENV] REFLEX_RUN_ID=%REFLEX_RUN_ID%

set "ENV_FILE=%ROOT%\.env"
echo [ENV] Loading .env from %ENV_FILE% ...

REM --- load .env (primary) then .env.local (overlay) into this process ---
call "%ROOT%\env.bat"
if errorlevel 1 (
  echo [ENV] ERROR loading env via %ROOT%\env.bat
  exit /b 1
)

REM --- always run the repo venv python, not a random one ---
set "PY=%ROOT%\.venv\Scripts\python.exe"
echo [RUN] %PY% evaluator\bots\FTS_rbf.py --env "%ENV_FILE%"

if "%FTS_RBF_PREFETCH_PCT_UP_MIN%"=="" set "FTS_RBF_PREFETCH_PCT_UP_MIN=0"
if "%FTS_RBF_PREFETCH_PCT_UP_HYSTERESIS%"=="" set "FTS_RBF_PREFETCH_PCT_UP_HYSTERESIS=0"
if "%FTS_RBF_PREFETCH_MIN_BARS%"=="" set "FTS_RBF_PREFETCH_MIN_BARS=5"
if "%FTS_RBF_PREFETCH_MIN_AGE_S%"=="" set "FTS_RBF_PREFETCH_MIN_AGE_S=120"
if "%FTS_RBF_PREFETCH_MAX_INFLIGHT%"=="" set "FTS_RBF_PREFETCH_MAX_INFLIGHT=4"

set "FTS_RBF_PREFETCH_PCT_UP_MIN=6"
set "FTS_RBF_PREFETCH_PCT_UP_HYSTERESIS=4"
set "FTS_RBF_PREFETCH_MIN_BARS=3"
set "FTS_RBF_PREFETCH_MIN_AGE_S=60"
set "FTS_RBF_PREFETCH_MAX_INFLIGHT=3"

set "FTS_RBF_EVENT_LOG_ENABLE=1"
set "FTS_RBF_EVENT_LOG_PATH=logs/fts_rbf_events.jsonl"
set "FTS_RBF_DEBUG_RVOL=1"

echo [ENV] FTS_RBF_PREFETCH_PCT_UP_MIN=%FTS_RBF_PREFETCH_PCT_UP_MIN%
echo [ENV] FTS_RBF_PREFETCH_PCT_UP_HYSTERESIS=%FTS_RBF_PREFETCH_PCT_UP_HYSTERESIS%
echo [ENV] FTS_RBF_PREFETCH_MIN_BARS=%FTS_RBF_PREFETCH_MIN_BARS%
echo [ENV] FTS_RBF_PREFETCH_MIN_AGE_S=%FTS_RBF_PREFETCH_MIN_AGE_S%
echo [ENV] FTS_RBF_PREFETCH_MAX_INFLIGHT=%FTS_RBF_PREFETCH_MAX_INFLIGHT%
echo [ENV] FTS_RBF_EVENT_LOG_ENABLE=%FTS_RBF_EVENT_LOG_ENABLE%
echo [ENV] FTS_RBF_EVENT_LOG_PATH=%FTS_RBF_EVENT_LOG_PATH%
echo [ENV] FTS_RBF_DEBUG_RVOL=%FTS_RBF_DEBUG_RVOL%

pushd "%ROOT%"
"%PY%" "evaluator\bots\FTS_rbf.py" --env "%ENV_FILE%"
popd

endlocal
