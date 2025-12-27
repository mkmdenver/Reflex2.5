@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM Reflex2.4 - Start EvalView Cockpit (Simple)
REM ============================================================

set "REPO_ROOT=%~dp0"
if "%REPO_ROOT:~-1%"=="\" set "REPO_ROOT=%REPO_ROOT:~0,-1%"

echo [PATH] REPO_ROOT = %REPO_ROOT%
cd /d "%REPO_ROOT%"

set "PY=%REPO_ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] venv not found at %REPO_ROOT%\.venv
  pause
  endlocal
  exit /b 1
)

set "PYTHONNOUSERSITE=1"
set "PYTHONPATH=%REPO_ROOT%"

if exist "%REPO_ROOT%\env.bat" (
  call "%REPO_ROOT%\env.bat" "%REPO_ROOT%\.env"
)

if "%EVALVIEW_HOST%"=="" set "EVALVIEW_HOST=0.0.0.0"
if "%EVALVIEW_PORT%"=="" set "EVALVIEW_PORT=7080"

echo [ENV] EVALVIEW_HOST=%EVALVIEW_HOST%
echo [ENV] EVALVIEW_PORT=%EVALVIEW_PORT%

echo [RUN] Starting EvalView (cockpit.evalview.app:app) ...
echo [RUN]   Browse: http://localhost:%EVALVIEW_PORT%/

"%PY%" -m uvicorn cockpit.evalview.app:app --host %EVALVIEW_HOST% --port %EVALVIEW_PORT%

echo.
echo [DONE] EvalView exited.
pause

endlocal
exit /b 0
