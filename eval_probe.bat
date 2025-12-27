@echo off
setlocal
cd /d %~dp0

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" tools\eval_probe.py %*
) else (
    python tools\eval_probe.py %*
)

endlocal
