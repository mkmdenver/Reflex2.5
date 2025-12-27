@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM Start the FILTER bot
REM Runs from project root and loads .env + .venv
REM ============================================================

set ROOT=%~dp0
cd /d %ROOT%

echo [PATH] ROOT = %ROOT%
echo [ENV] Loading .env ...

for /f "usebackq tokens=*" %%a in ("%ROOT%\.env") do set %%a

if not exist "%ROOT%\.venv\Scripts\activate.bat" (
    echo [VENV] Creating virtual environment...
    python -m venv .venv
)

echo [VENV] Activating virtual environment...
call "%ROOT%\.venv\Scripts\activate.bat"

echo [RUN] Starting FILTER bot...
python -m evaluator.bots.FTS_simple1

endlocal
