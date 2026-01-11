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
  echo Usage: %~nx0 SYMBOL KIND YYYY-MM-DD
  exit /b 1
)

set "SYMBOL=%~1"
set "KIND=%~2"
set "SINCE=%~3"

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

cd /d "%ROOT%"
call "%ROOT%\env.bat"

set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "MODULE=tools.symbol_manager.db_backfill"

echo [RUN] %KIND% backfill %SYMBOL% since %SINCE%
"%PY%" -m %MODULE% --kind %KIND% --symbol %SYMBOL% --since %SINCE%
set "RC=%errorlevel%"

if not "%RC%"=="0" (
  echo [ERROR] Backfill failed with exit code %RC%
  endlocal & exit /b %RC%
)

echo [DONE] Backfill complete.
endlocal
exit /b 0
