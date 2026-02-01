@echo off
setlocal

REM Resolve ROOT as two levels up from this .bat (…\evaluator\bots -> project root)
set SCRIPT_DIR=%~dp0
cd /d %SCRIPT_DIR%
cd ..\..
set ROOT=%CD%

echo [PATH] ROOT = %ROOT%
echo [ENV] Loading .env from %ROOT%\.env ...

REM Activate shared virtualenv at ROOT\.venv if it exists
if exist "%ROOT%\.venv\Scripts\activate.bat" (
    call "%ROOT%\.venv\Scripts\activate.bat"
)

REM Run the filter stream tap (Python file at evaluator\bots\Filter_tap.py)
python -m evaluator.bots.Filter_tap

endlocal
