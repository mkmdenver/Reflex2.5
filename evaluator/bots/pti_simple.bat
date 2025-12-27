@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM Start the SIMPLE PATTERN/INTENT bot (PSI_simple1)
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

if "%REFLEX_RUN_ID%"=="" set "REFLEX_RUN_ID=run0"
echo [ENV] REFLEX_RUN_ID=%REFLEX_RUN_ID%


for /f "usebackq tokens=*" %%a in ("%ROOT%\.env") do set %%a

if not exist "%ROOT%\.venv\Scripts\activate.bat" (
    echo [VENV] Creating virtual environment at %ROOT%\.venv ...
    python -m venv "%ROOT%\.venv"
)

echo [VENV] Activating virtual environment...
call "%ROOT%\.venv\Scripts\activate.bat"

echo [RUN] Starting SIMPLE PATTERN/INTENT bot (PSI_simple1)...
cd /d "%ROOT%"
python -m evaluator.bots.PTI_simple

popd
endlocal
