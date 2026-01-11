@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM Reflex2 - Backfill DAILY bars (ALL symbols) since a date
REM
REM Usage:
REM   backfill_daily_since YYYY-MM-DD
REM ============================================================

if "%~1"=="" (
  echo Usage: %~nx0 YYYY-MM-DD
  exit /b 1
)

set "SINCE=%~1"
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

cd /d "%ROOT%"
call "%ROOT%\env.bat"

set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "MODULE=tools.symbol_manager.db_backfill"

echo [RUN] DAILY backfill ALL since %SINCE%
"%PY%" -m %MODULE% --kind daily --symbol ALL --since %SINCE%
set "RC=%errorlevel%"

if not "%RC%"=="0" (
  echo [ERROR] DAILY backfill failed with exit code %RC%
  endlocal & exit /b %RC%
)

echo [DONE] DAILY backfill complete.
endlocal
exit /b 0
