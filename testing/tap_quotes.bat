@echo off
setlocal
cd /d %~dp0

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" tools\tap_quotes.py %*
) else (
    python tools\tap_quotes.py %*
)

endlocal
