@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM Reflex2 - Backfill MINUTE bars (ALL symbols) since a date
REM
REM Usage:
REM   backfil_min_since YYYY-MM-DD [UNTIL_YYYY-MM-DD]
REM
REM If UNTIL is omitted, uses today (local) as YYYY-MM-DD.
REM ============================================================

if "%~1"=="" (
  echo Usage: %~nx0 YYYY-MM-DD [UNTIL_YYYY-MM-DD]
  exit /b 1
)

set "SINCE=%~1"
set "UNTIL=%~2"

if "%UNTIL%"=="" (
  for /f %%D in ('powershell -NoProfile -Command "(Get-Date).ToString(\"yyyy-MM-dd\")"') do set "UNTIL=%%D"
)

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

cd /d "%ROOT%"
call "%ROOT%\env.bat"

set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "MODULE=tools.symbol_manager.db_backfill"

echo [RUN] MINUTE backfill ALL from %SINCE% to %UNTIL%
"%PY%" -m %MODULE% --kind minute --symbol ALL --since %SINCE% --until %UNTIL%
set "RC=%errorlevel%"

if not "%RC%"=="0" (
  echo [ERROR] MINUTE backfill failed with exit code %RC%
  endlocal & exit /b %RC%
)

echo [DONE] MINUTE backfill complete.
endlocal
exit /b 0
