@echo off
setlocal EnableExtensions

REM ------------------------------------------------------------
REM pti_1barup.bat  (run from anywhere)
REM Lives in evaluator\bots\
REM KISS: resolve ROOT, load .env + .env.local, run venv python.
REM ------------------------------------------------------------

echo PTI_1barup start up

REM Resolve repo root (this bat lives in evaluator\bots)
set "SCRIPT_DIR=%~dp0"
set "ROOT=%SCRIPT_DIR%..\.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"

echo [PATH] ROOT=%ROOT%

REM Deterministic python from repo venv
set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] Missing venv python: "%PY%"
  echo         Create it at repo root: python -m venv .venv
  exit /b 1
)

pushd "%ROOT%"

REM Load .env then .env.local
if not exist "%ROOT%\env.bat" (
  echo [ERROR] Missing "%ROOT%\env.bat"
  popd
  exit /b 1
)

if not exist "%ROOT%\.env" (
  echo [ERROR] Missing "%ROOT%\.env"
  popd
  exit /b 1
)

echo [ENV] Loading .env from %ROOT%\.env ...
call "%ROOT%\env.bat" "%ROOT%\.env"
if errorlevel 1 (
  echo [ERROR] env.bat failed on "%ROOT%\.env"
  popd
  exit /b 1
)

if exist "%ROOT%\.env.local" (
  echo [ENV] Loading .env.local from %ROOT%\.env.local ...
  call "%ROOT%\env.bat" "%ROOT%\.env.local"
  if errorlevel 1 (
    echo [ERROR] env.bat failed on "%ROOT%\.env.local"
    popd
    exit /b 1
  )
)
set "PTI_1BAR_SYMBOLS=SPY,BNAI,MSFT"
set "PTI_1BAR_REQUEST_WARM_TIER=1"
set "PTI_1BAR_WARM_TIER_NAME=WARM"
set "PTI_ACCOUNT_ID=alpaca:paper"


echo [RUN] %PY% evaluator\bots\PTI_1barup.py
"%PY%" "evaluator\bots\PTI_1barup.py"
set "RC=%ERRORLEVEL%"

popd
exit /b %RC%
