@echo off
setlocal
cd /d %~dp0

REM Use the project venv Python if it exists
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" tools\tap_ticks.py %*
) else (
    python tools\tap_ticks.py %*
)

endlocal
