@echo off
setlocal
cd /d %~dp0

REM ==========================================================
REM Reflex2.3 - Start Evaluator Combo (Ross bull-flag loop)
REM
REM Assumes:
REM   - This .bat lives in c:\Projects\Reflex2.3
REM   - .env is in this directory (loaded by the Python code)
REM   - evaluator package is installed in .venv
REM   - DataHub + Garnet already running
REM
REM Usage:
REM   start_combo_ross
REM   start_combo_ross --debug   (example extra args)
REM ==========================================================

echo ================== Reflex Evaluator (Combo Ross) ==================
echo   CWD: %CD%
echo   Module: evaluator.loop_combo_ross
echo   .env  : %CD%\.env
echo ===================================================================
echo.

REM Prefer project virtualenv Python if it exists
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m evaluator.loop_combo_ross %*
) else (
    python -m evaluator.loop_combo_ross %*
)

endlocal
