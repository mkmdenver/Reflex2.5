@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "ROOT=%~dp0"
cd /d "%ROOT%" || exit /b 1

if not exist logs mkdir logs

REM Unique log each run (no lock conflicts)
set "STAMP=%DATE:~-4%%DATE:~4,2%%DATE:~7,2%_%TIME:~0,2%%TIME:~3,2%%TIME:~6,2%"
set "STAMP=%STAMP: =0%"
set "LOG=%ROOT%logs\verify_fill_%STAMP%.log"

echo [INFO] Logging to: %LOG%
echo [INFO] Started: %DATE% %TIME%
echo ============================================================>> "%LOG%"
echo Started: %DATE% %TIME%>> "%LOG%"
echo ROOT: %ROOT%>> "%LOG%"
echo ============================================================>> "%LOG%"

REM Run the real script; write to log and screen
call "%ROOT%run_verify_fill_3steps.bat" > "%LOG%" 2>&1

set "RC=%ERRORLEVEL%"

echo ============================================================>> "%LOG%"
echo Finished: %DATE% %TIME%  exit=%RC%>> "%LOG%"
echo ============================================================>> "%LOG%"

REM Show log after completion (so you always see something)
type "%LOG%"

exit /b %RC%
