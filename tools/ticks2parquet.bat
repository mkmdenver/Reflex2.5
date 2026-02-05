@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ------------------------------------------------------------
REM ticks2parquet.bat (Reflex2.4)
REM
REM Examples:
REM   ticks2parquet --since 2025-07-28 --until 2025-11-14
REM   ticks2parquet --since 2025-10-01 --until 2025-10-10 --symbol ACCO
REM ------------------------------------------------------------

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
pushd "%ROOT%"

echo [RUNNING] %~f0
set "ENV_FILE=%ROOT%\.env"
set "PY=%ROOT%\.venv\Scripts\python.exe"

REM --- Create venv deterministically (3.12) if missing ---
if not exist "%PY%" (
  echo [SETUP] Creating .venv with Python 3.12...
  py -3.12 -m venv "%ROOT%\.venv"
  if errorlevel 1 (
    echo [ERROR] Failed to create venv with py -3.12
    popd
    exit /b 1
  )

  echo [SETUP] Installing requirements...
  "%PY%" -m pip install --upgrade pip
  if exist "%ROOT%\requirements.txt" (
    "%PY%" -m pip install -r "%ROOT%\requirements.txt"
  ) else (
    echo [WARN] requirements.txt not found at "%ROOT%\requirements.txt"
  )
)

REM --- Load .env via env.bat (single source of truth) ---
if exist "%ROOT%\env.bat" (
  call "%ROOT%\env.bat" "%ENV_FILE%"
  if errorlevel 1 (
    echo [ERROR] env.bat failed. Expected: call env.bat path\to\.env
    popd
    exit /b 1
  )
) else (
  echo [ERROR] env.bat not found at "%ROOT%\env.bat"
  popd
  exit /b 1
)

REM --- Prevent user-site (3.14 roaming packages) from leaking in ---
set "PYTHONNOUSERSITE=1"

REM --- Ensure repo root on module path ---
set "PYTHONPATH=%ROOT%"

echo [RUN] tools.symbol_manager.export_ticks_to_parquet %*
"%PY%" -m tools.symbol_manager.export_ticks_to_parquet %*

popd
endlocal
exit /b 0
