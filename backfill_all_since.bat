@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM Reflex2 - Backfill ALL (daily + minute + tick) since a date
REM
REM Usage:
REM    backfill_all_since YYYY-MM-DD
REM ============================================================

if "%~1"=="" (
  echo Usage: %~nx0 YYYY-MM-DD
  exit /b 1
)

set "SINCE=%~1"
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

cd /d "%ROOT%"

REM Load .env then .env.local into this process
call "%ROOT%\env.bat"

REM Resolve python (prefer project venv)
set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  set "PY=%ROOT%.venv\Scripts\python.exe"
)
if not exist "%PY%" (
  set "PY=python"
)

set "MODULE=tools.symbol_manager.db_backfill"

echo [PATH] ROOT=%ROOT%
echo [ENV] Using PY=%PY%
echo.

echo [RUN] DAILY backfill ALL since %SINCE%
"%PY%" -m %MODULE% --kind daily --symbol ALL --since %SINCE%
if errorlevel 1 exit /b %errorlevel%

echo.
echo [RUN] MINUTE backfill ALL since %SINCE%
"%PY%" -m %MODULE% --kind minute --symbol ALL --since %SINCE%
if errorlevel 1 exit /b %errorlevel%

echo.
echo [RUN] TICK backfill ALL since %SINCE%
"%PY%" -m %MODULE% --kind tick --symbol ALL --since %SINCE%
if errorlevel 1 exit /b %errorlevel%

echo.
echo [DONE] Backfill ALL complete.
endlocal
exit /b 0
