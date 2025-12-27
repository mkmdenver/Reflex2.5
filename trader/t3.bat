@echo off
setlocal
title REFLEX :: BROKER WORKER (queues)
cd /d C:\Projects\Reflex2.2
set PYTHONPATH=C:\Projects\Reflex2.2

if not defined GARNET_URL set GARNET_URL=redis://127.0.0.1:6379/0
echo [ENV] GARNET_URL=%GARNET_URL%
echo [RUN] trader.broker_worker
".\.venv\Scripts\python.exe" -X dev -m trader.broker_worker
echo.
echo [EXIT] Worker returned %ERRORLEVEL%
echo.
echo Press any key to close this window...
pause >nul
