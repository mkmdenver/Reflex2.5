@echo off
setlocal EnableExtensions

REM ============================================================
REM Reflex2 - Backfill 3 steps (daily -> minute -> tick)
REM Creates a unique per-run log; runs sequentially (no parallel).
REM Usage:
REM   backfill_3steps.bat  [SYMBOL|ALL]  YYYY-MM-DD  [YYYY-MM-DD]
REM Example:
REM   backfill_3steps.bat ALL 2025-12-10 2025-12-23
REM ============================================================

REM ---- args ----
set "SYM=%~1"
set "SINCE=%~2"
set "UNTIL=%~3"

if "%SYM%"=="" set "SYM=ALL"
if "%SINCE%"=="" (
  echo [ERROR] Usage: backfill_3steps.bat [SYMBOL^|ALL] YYYY-MM-DD [YYYY-MM-DD]
  exit /b 2
)

REM ---- find repo root by walking up until .env is found ----
set "CUR=%~dp0"
for %%I in ("%CUR%.") do set "CUR=%%~fI"

set "REPO="
set /a HOPS=0

:find_root
if exist "%CUR%\.env" (
  set "REPO=%CUR%"
  goto :root_found
)

set /a HOPS+=1
if %HOPS% GEQ 12 goto :root_not_found

for %%P in ("%CUR%\..") do set "CUR=%%~fP"
goto :find_root

:root_not_found
echo [ERROR] Could not find repo root (.env) by walking up from: %~dp0
exit /b 2

:root_found
cd /d "%REPO%" || exit /b 2

REM ---- choose python (prefer repo .venv) ----
set "PY=%REPO%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

REM ---- make log dir ----
if not exist "%REPO%\logs\backfill" mkdir "%REPO%\logs\backfill" >nul 2>&1

REM ---- timestamp (no WMIC; locale-safe-ish) ----
set "STAMP=%DATE%_%TIME%"
set "STAMP=%STAMP:/=-%"
set "STAMP=%STAMP::=-%"
set "STAMP=%STAMP:.=-%"
set "STAMP=%STAMP: =0%"

set "LOG=%REPO%\logs\backfill\backfill_%STAMP%_%RANDOM%.log"

echo [INFO] REPO=%REPO%
echo [INFO] PY=%PY%
echo [INFO] LOG=%LOG%
echo [INFO] symbol=%SYM% since=%SINCE% until=%UNTIL%
echo.

>>"%LOG%" echo ============================================================
>>"%LOG%" echo [START] %DATE% %TIME%
>>"%LOG%" echo [REPO ] %REPO%
>>"%LOG%" echo [PY   ] %PY%
>>"%LOG%" echo [ARGS ] symbol=%SYM% since=%SINCE% until=%UNTIL%
>>"%LOG%" echo ============================================================

call :do_kind daily
if errorlevel 1 goto :fail

call :do_kind minute
if errorlevel 1 goto :fail

call :do_kind tick
if errorlevel 1 goto :fail

>>"%LOG%" echo ============================================================
>>"%LOG%" echo [DONE ] %DATE% %TIME% exit=0
>>"%LOG%" echo ============================================================

type "%LOG%"
exit /b 0

:fail
set "RC=%ERRORLEVEL%"
>>"%LOG%" echo ============================================================
>>"%LOG%" echo [FAIL ] %DATE% %TIME% exit=%RC%
>>"%LOG%" echo ============================================================
type "%LOG%"
exit /b %RC%

:do_kind
set "KIND=%~1"
echo [RUN] %KIND%
>>"%LOG%" echo.
>>"%LOG%" echo [RUN] kind=%KIND% symbol=%SYM% since=%SINCE% until=%UNTIL%

if "%UNTIL%"=="" (
  "%PY%" -m tools.symbol_manager.db_backfill --kind %KIND% --symbol "%SYM%" --since "%SINCE%" >>"%LOG%" 2>&1
) else (
  "%PY%" -m tools.symbol_manager.db_backfill --kind %KIND% --symbol "%SYM%" --since "%SINCE%" --until "%UNTIL%" >>"%LOG%" 2>&1
)

exit /b %ERRORLEVEL%
