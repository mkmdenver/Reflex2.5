@echo off
setlocal
cd /d %~dp0

REM Launch Ross-style filter that listens to hub ticks
REM and promotes symbols / publishes to the Ross stream.

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" tools\ross_filter.py %*
) else (
    python tools\ross_filter.py %*
)

endlocal
