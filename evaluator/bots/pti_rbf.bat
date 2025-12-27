@echo off
setlocal EnableExtensions

REM ------------------------------------------------------------
REM pti_rbf_bot.bat  (run from anywhere)
REM ------------------------------------------------------------

REM Resolve repo root (this bat lives in evaluator\bots)
set SCRIPT_DIR=%~dp0
set ROOT=%SCRIPT_DIR%..\..
for %%I in ("%ROOT%") do set ROOT=%%~fI

REM Default identity
if "%REFLEX_MODE%"=="" set REFLEX_MODE=LIVE
if "%REFLEX_INSTANCE_ID%"=="" set REFLEX_INSTANCE_ID=liveA
if "%REFLEX_RUN_ID%"=="" set REFLEX_RUN_ID=run0

echo [ENV] REFLEX_MODE=%REFLEX_MODE%
echo [ENV] REFLEX_INSTANCE_ID=%REFLEX_INSTANCE_ID%
echo [ENV] REFLEX_RUN_ID=%REFLEX_RUN_ID%
echo [PATH] ROOT=%ROOT%

REM Use repo venv
set PY=%ROOT%\.venv\Scripts\python.exe
if not exist "%PY%" (
  echo [ERR] Repo venv not found: "%PY%"
  echo       Create it at repo root: python -m venv .venv
  exit /b 1
)

pushd "%ROOT%"

echo [ENV] Loading .env from %ROOT%\.env ...
echo [RUN] %PY% evaluator\bots\PTI_rbf.py
"%PY%" "evaluator\bots\PTI_rbf.py"
set RC=%ERRORLEVEL%

popd
exit /b %RC%
