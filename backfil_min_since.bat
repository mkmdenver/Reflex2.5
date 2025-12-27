@echo off
setlocal ENABLEDELAYEDEXPANSION

REM === CONFIG ===
set "REFLEX_ROOT=C:\Projects\Reflex2.3"

if "%~1"=="" (
    echo Usage: %~nx0 YYYY-MM-DD
    echo Example: %~nx0 2025-01-01
    goto :eof
)

set "SINCE=%~1"

REM Get today's date as YYYY-MM-DD for --until
for /f %%D in ('powershell -NoProfile -Command "(Get-Date).ToString(\"yyyy-MM-dd\")"') do set "UNTIL=%%D"

echo [INFO] Backfilling MINUTE BARS from %SINCE% to %UNTIL%

cd /d "%REFLEX_ROOT%"
call ".venv\Scripts\activate.bat"

python -m symbol_manager.db_backfill_min --since %SINCE% --until %UNTIL%

endlocal
