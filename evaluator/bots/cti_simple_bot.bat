@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM Start the SIMPLE COMBO/INTENT bot (CTI_simple1)
REM Lives in:   evaluator\bots
REM Runs from:  project ROOT and loads ROOT\.env + ROOT\.venv
REM ============================================================

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%\..\.."
set "ROOT=%CD%"

echo [PATH] ROOT = %ROOT%
echo [ENV] Loading .env from %ROOT%\.env ...

if not exist "%ROOT%\.env" (
    echo [ERROR] .env not found at %ROOT%\.env
    popd
    endlocal
    goto :EOF
)

for /f "usebackq tokens=*" %%a in ("%ROOT%\.env") do set %%a

if not exist "%ROOT%\.venv\Scripts\activate.bat" (
    echo [VENV] Creating virtual environment at %ROOT%\.venv ...
    python -m venv "%ROOT%\.venv"
)

echo [VENV] Activating virtual environment...
call "%ROOT%\.venv\Scripts\activate.bat"

echo [RUN] Starting SIMPLE COMBO/INTENT bot (CTI_simple1)...
cd /d "%ROOT%"
python -m evaluator.bots.CTI_simple1

popd
endlocal
