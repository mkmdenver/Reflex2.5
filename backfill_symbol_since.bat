@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM Reflex2 - Backfill ONE SYMBOL since a date
REM
REM Usage:
REM   backfill_symbol_since SYMBOL KIND YYYY-MM-DD
REM
REM Examples:
REM   backfill_symbol_since SPY daily  2021-01-01
REM   backfill_symbol_since SPY minute 2021-01-01
REM   backfill_symbol_since SPY tick   2025-08-01
REM ============================================================

if "%~3"=="" (
  echo Usage: backfill_symbol_since SYMBOL KIND YYYY-MM-DD
  exit /b 1
)

set "SYMBOL=%~1"
set "KIND=%~2"
set "SINCE=%~3"

REM ------------------------------------------------------------
REM Resolve repo root
REM ------------------------------------------------------------
set "REPO_ROOT=%~dp0"
cd /d "%REPO_ROOT%"

echo [PATH] REPO_ROOT = %REPO_ROOT%

REM ------------------------------------------------------------
REM Load .env
REM ------------------------------------------------------------
if exist ".env" (
  echo [ENV] Loading .env ...
  for /F "usebackq tokens=1,* delims==" %%A in (".env") do (
    set "line=%%A"
    if not "!line!"=="" if "!line:~0,1!" NEQ "#" (
      set "%%A=%%B"
    )
  )
) else (
  echo [WARN] .env not found
)

REM ------------------------------------------------------------
REM Ensure venv + requirements
REM ------------------------------------------------------------
set "VENV_DIR=%REPO_ROOT%\.venv"
set "PY=%VENV_DIR%\Scripts\python.exe"

if not exist "%PY%" (
  echo [VENV] Creating virtualenv at %VENV_DIR% ...
  python -m venv "%VENV_DIR%"
)

echo [VENV] Using %PY%

if exist "requirements.txt" (
  echo [VENV] Installing requirements...
  "%PY%" -m pip install -r requirements.txt
)

REM ------------------------------------------------------------
REM Run backfill
REM ------------------------------------------------------------
echo.
echo [RUN] %KIND% backfill %SYMBOL% since %SINCE%

"%PY%" -m tools.symbol_manager.db_backfill ^
  --kind %KIND% ^
  --symbol %SYMBOL% ^
  --since %SINCE%

if errorlevel 1 (
  echo [ERROR] Backfill failed.
  exit /b 1
)

echo [DONE] Backfill complete.
exit /b 0
