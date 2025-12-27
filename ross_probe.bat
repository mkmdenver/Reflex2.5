@echo off
setlocal
cd /d %~dp0

REM Launch lightweight probe that listens to the Ross specialty stream
REM and sends simple trade intents into Trader (similar to eval_probe).

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" tools\ross_probe.py %*
) else (
    python tools\ross_probe.py %*
)

endlocal
