@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM --- repo root: this bat lives in <root>\evaluator\bots ---
set "ROOT=%~dp0..\.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"

echo [PATH] ROOT=%ROOT%

REM --- set identity ---
if "%REFLEX_MODE%"=="" set "REFLEX_MODE=LIVE"
if "%REFLEX_INSTANCE_ID%"=="" set "REFLEX_INSTANCE_ID=liveA"
if "%REFLEX_RUN_ID%"=="" set "REFLEX_RUN_ID=run0"

echo [ENV] REFLEX_MODE=%REFLEX_MODE%
echo [ENV] REFLEX_INSTANCE_ID=%REFLEX_INSTANCE_ID%
echo [ENV] REFLEX_RUN_ID=%REFLEX_RUN_ID%

set "ENV_FILE=%ROOT%\.env"
echo [ENV] Loading .env from %ENV_FILE% ...

REM --- always run the repo venv python, not a random one ---
set "PY=%ROOT%\.venv\Scripts\python.exe"
echo [RUN] %PY% evaluator\bots\FTS_rbf.py --env "%ENV_FILE%"

pushd "%ROOT%"
"%PY%" "evaluator\bots\FTS_rbf.py" --env "%ENV_FILE%"
popd

endlocal
