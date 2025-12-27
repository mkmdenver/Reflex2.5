@echo off
setlocal
cd /d %~dp0

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" tools\debug_publish_ross.py %*
) else (
    python tools\debug_publish_ross.py %*
)

endlocal
