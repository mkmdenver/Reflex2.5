@echo off
setlocal
cd /d %~dp0

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" tools\cameron_probe.py %*
) else (
    python tools\cameron_probe.py %*
)

endlocal
