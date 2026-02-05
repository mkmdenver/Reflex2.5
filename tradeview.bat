@echo off
setlocal EnableExtensions

REM --- This BAT lives in repo root ---
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

REM --- Load .env + .env.local into THIS process ---
if not exist "%ROOT%\env.bat" (
  echo [ERROR] Missing "%ROOT%\env.bat"
  exit /b 1
)
call "%ROOT%\env.bat"
if errorlevel 1 exit /b 1

REM Defaults
if "%TRADEVIEW_PORT%"=="" set "TRADEVIEW_PORT=7012"

REM Choose app module (keep this dead-simple & explicit)
set "APP=cockpit.tradeview.main:app"
if exist "%ROOT%\tradeview\main.py" set "APP=tradeview.main:app"
if exist "%ROOT%\tradeview\main.py" set "APP=tradeview.main:app"

echo.
echo ================== ENV SUMMARY (TradeView) ==================
echo REFLEX_INSTANCE_ID  : %REFLEX_INSTANCE_ID%
echo REDIS               : %GARNET_URL%
echo TRADEVIEW_PORT      : %TRADEVIEW_PORT%
echo TRADER_API_PORT     : %TRADER_API_PORT%
echo APP                 : %APP%
echo ROOT                : %CD%
echo ==============================================================
echo.

if not exist "%ROOT%\.venv\Scripts\python.exe" (
  echo [ERROR] Missing venv python: "%ROOT%\.venv\Scripts\python.exe"
  exit /b 1
)

echo Launching TradeView on http://127.0.0.1:%TRADEVIEW_PORT%/
echo.

"%ROOT%\.venv\Scripts\python.exe" -m uvicorn "%APP%" --host 127.0.0.1 --port %TRADEVIEW_PORT%

endlocal & exit /b %ERRORLEVEL%
