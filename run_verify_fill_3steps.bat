@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM Reflex2 - Run 3-step backfill with LIVE console + LOG file
REM
REM - Runs the existing backfill BAT (or python) and tees output
REM   to logs\verify_fill.log while still showing it live.
REM - Pure CMD (no PowerShell needed).
REM ============================================================

set "ROOT=%~dp0"
cd /d "%ROOT%" || exit /b 1

if not exist logs mkdir logs
set "LOG=%ROOT%logs\verify_fill.log"

echo [INFO] ROOT=%ROOT%
echo [INFO] Logging to: %LOG%
echo [INFO] Started: %DATE% %TIME%
echo ============================================================>> "%LOG%"
echo Started: %DATE% %TIME%>> "%LOG%"
echo ROOT: %ROOT%>> "%LOG%"
echo ============================================================>> "%LOG%"

REM --- run the backfill BAT by explicit path and tee output ---
call "%ROOT%run_verify_fill_3steps.bat" 2>&1 | call "%ROOT%tools\tee.cmd" "%LOG%"

set "RC=%ERRORLEVEL%"
echo ============================================================>> "%LOG%"
echo Finished: %DATE% %TIME%  exit=%RC%>> "%LOG%"
echo ============================================================>> "%LOG%"

echo [DONE] exit=%RC%
exit /b %RC%
