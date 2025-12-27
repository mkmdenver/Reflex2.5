@echo off
setlocal enableextensions

REM Change to repo root (directory of this script)
cd /d "%~dp0"

echo.

REM ---- Load shared env (reads .env etc.) ----
if exist "env.bat" (
    echo [ENV] Loading env.bat ...
    call env.bat
) else (
    echo [ENV] env.bat not found, continuing with current env...
)

REM ---- Ensure venv exists ----
if not exist ".venv\Scripts\python.exe" (
    echo [BOOT] Creating Python venv at .venv ...
    py -3 -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create venv
        endlocal & exit /b 1
    )
    echo [PKGS] Upgrading pip in venv ...
    ".venv\Scripts\python.exe" -m pip install -U pip
)

REM ---- Install requirements from root ----
echo [PKGS] Installing requirements from requirements.txt ...
".venv\Scripts\pip.exe" install -r "requirements.txt"

REM ---- Ports / instance ----
if not defined COCKPIT_PORT set COCKPIT_PORT=7010
if not defined TRADER_API_PORT set TRADER_API_PORT=7002

echo.
echo ================== ENV SUMMARY (BrokerView) ==================
echo INSTANCE            : %INSTANCE%
echo REDIS               : %GARNET_URL%
echo BROKERVIEW_PORT     : %COCKPIT_PORT%
echo TRADER_API_PORT     : %TRADER_API_PORT%
echo ROOT                : %CD%
echo ==============================================================
echo.
echo Launching Broker Cockpit on http://127.0.0.1:%COCKPIT_PORT%/
echo.

".venv\Scripts\python.exe" -m uvicorn "cockpit.brokerview.main:app" --host 0.0.0.0 --port %COCKPIT_PORT%

set EC=%ERRORLEVEL%
endlocal & exit /b %EC%
