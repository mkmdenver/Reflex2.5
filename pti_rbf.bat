@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM Start the PATTERN bot
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

echo [RUN] Starting PATTERN bot...
python -m evaluator.bots.PTI_rbf

endlocal
