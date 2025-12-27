@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >NUL

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
pushd "%ROOT%"

REM venv python (NO embedded quotes)
set "PY=%ROOT%\.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo [ERROR] venv python not found: %PY%
  popd
  exit /b 1
)

REM Clean, deterministic runtime
set "PYTHONNOUSERSITE=1"
set "PYTHONPATH=%ROOT%"

REM Load .env if you use env.bat
if exist "%ROOT%\env.bat" (
  call "%ROOT%\env.bat" "%ROOT%\.env"
)

REM Port default
if not defined EVALUATOR_API_PORT set "EVALUATOR_API_PORT=7001"

echo [PATH] ROOT=%ROOT%
echo [ENV]  PY=%PY%
echo [ENV]  PYTHONPATH=%PYTHONPATH%
echo [ENV]  EVALUATOR_API_PORT=%EVALUATOR_API_PORT%
echo.

REM Start evaluator API
echo [LAUNCH] Evaluator API on :%EVALUATOR_API_PORT% (evaluator.app:app)
start "Evaluator:%EVALUATOR_API_PORT%" cmd /k ""%PY%" -m uvicorn evaluator.app:app --host 0.0.0.0 --port %EVALUATOR_API_PORT%"

popd
exit /b 0
