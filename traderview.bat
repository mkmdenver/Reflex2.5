@echo off
setlocal EnableExtensions

REM =========================================================
REM traderview.bat  (Reflex2.5)  KISS launcher
REM Lives in repo root. Runs cockpit\traderview backend (uvicorn).
REM Mirrors trader.bat env loading + venv resolution.
REM =========================================================

REM --- Root folder (this BAT lives in repo root) ---
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

echo [RUNNING] "%~f0"
echo [ENV] ROOT=%ROOT%

pushd "%ROOT%"

REM --- Load .env + .env.local into THIS process ---
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

call "%ROOT%\env.bat" "%ROOT%\.env"
if errorlevel 1 (
  echo [ERROR] env.bat failed on "%ROOT%\.env"
  popd
  exit /b 1
)

if exist "%ROOT%\.env.local" (
  call "%ROOT%\env.bat" "%ROOT%\.env.local"
  if errorlevel 1 (
    echo [ERROR] env.bat failed on "%ROOT%\.env.local"
    popd
    exit /b 1
  )
)

REM --- Deterministic python ---
set "PY=%ROOT%\.venv\Scripts\python.exe"
echo [ENV] PY=%PY%
if not exist "%PY%" (
  echo [ERROR] Missing venv python: "%PY%"
  popd
  exit /b 3
)

REM --- Defaults ---
if not defined TRADERVIEW_PORT set "TRADERVIEW_PORT=7011"

REM --- Sanity: ensure traderview backend exists ---
if not exist "%ROOT%\cockpit\traderview\main.py" (
  echo [ERROR] Missing "%ROOT%\cockpit\traderview\main.py"
  popd
  exit /b 1
)

echo [RUN] TraderView on %TRADERVIEW_PORT%
start "TraderView:%TRADERVIEW_PORT%" cmd /k ""%PY%" -m uvicorn cockpit.traderview.main:app --host 0.0.0.0 --port %TRADERVIEW_PORT%"

start "" "http://127.0.0.1:%TRADERVIEW_PORT%/"

popd
endlocal
exit /b 0
