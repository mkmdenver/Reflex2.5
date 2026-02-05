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
if "%ENGINEERVIEW_PORT%"=="" set "ENGINEERVIEW_PORT=7013"

echo.
echo ================= ENV SUMMARY (EngineerView) =================
echo REFLEX_INSTANCE_ID  : %REFLEX_INSTANCE_ID%
echo REDIS               : %GARNET_URL%
echo ENGINEERVIEW_PORT   : %ENGINEERVIEW_PORT%
echo TRADER_API_PORT     : %TRADER_API_PORT%
echo ROOT                : %CD%
echo ==============================================================
echo.

if not exist "%ROOT%\.venv\Scripts\python.exe" (
  echo [ERROR] Missing venv python: "%ROOT%\.venv\Scripts\python.exe"
  exit /b 1
)

echo Launching EngineerView on http://127.0.0.1:%ENGINEERVIEW_PORT%/
echo.

"%ROOT%\.venv\Scripts\python.exe" -m uvicorn "cockpit.engineerview.main:app" --host 127.0.0.1 --port %ENGINEERVIEW_PORT%

endlocal & exit /b %ERRORLEVEL%
