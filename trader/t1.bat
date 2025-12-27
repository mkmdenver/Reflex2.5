@echo off
setlocal ENABLEDELAYEDEXPANSION
title REFLEX :: TRADER (port 7002)

REM --- repo root ---
cd /d C:\Projects\Reflex2.2

REM --- env summary (stay on screen) ---
echo [ENV] INSTANCE=%REFLEX__INSTANCE%
echo [ENV] LOG_LEVEL=%LOG_LEVEL%
echo [ENV] GARNET_URL=%GARNET_URL%
echo [ENV] BROKER_DATABASE_URL=%BROKER_DATABASE_URL%
echo.

REM --- force pythonpath to repo root ---
set PYTHONPATH=C:\Projects\Reflex2.2

REM --- ensure venv python exists ---
if not exist .\.venv\Scripts\python.exe (
  echo [ERROR] .venv\Scripts\python.exe not found. Create venv and install deps.
  echo   python -m venv .venv
  echo   .\.venv\Scripts\pip install -r requirements.txt
  goto :END
)

REM --- do NOT autostart worker in-process so we can see it separately ---
set TRADER_START_WORKER=0

echo [RUN] Uvicorn trader.app:app on 7002
".\.venv\Scripts\python.exe" -X dev -m uvicorn trader.app:app --reload --host 127.0.0.1 --port 7002
echo.
echo [EXIT] Trader process returned with code %ERRORLEVEL%
:END
echo.
echo Press any key to close this window...
pause >nul
