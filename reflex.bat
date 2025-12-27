@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

echo [PATH] ROOT = %ROOT%

if exist "%ROOT%\env.bat" (
  call "%ROOT%\env.bat" "%ROOT%\.env"
)

echo [START] DataHub...
start "DataHub" "%ROOT%\datahub.bat"

echo [START] Evaluator services...
start "Evaluator" "%ROOT%\eval.bat"

echo [START] EvalView cockpit...
start "EvalView" "%ROOT%\evalview.bat"

echo [START] intent_tap...
start "intent_tap" "%ROOT%\evaluator\bots\intent_tap.bat"

echo.
echo [DONE] Reflex stack launch commands issued.
echo.

endlocal
exit /b 0
