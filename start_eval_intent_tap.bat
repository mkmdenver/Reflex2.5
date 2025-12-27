@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM  Reflex2 - Start Evaluator Intent Tap
REM
REM  Usage:
REM      start_eval_intent_tap
REM
REM  Assumes:
REM    - You run this from project root (c:\Projects\Reflex2.3)
REM    - env.bat exists in project root and sets REPO_ROOT, etc.
REM    - .venv in project root is the Python venv for Reflex2
REM ============================================================

REM Move to repo root (folder containing this .bat)
set "REPO_ROOT=%~dp0"
cd /d "%REPO_ROOT%"

REM Load environment (.env, PYTHONPATH, etc.)
if exist "env.bat" (
    call "env.bat"
) else (
    echo [ERROR] env.bat not found in %REPO_ROOT%
    goto :eof
)

REM Ensure virtualenv exists
if not exist ".venv" (
    echo [INFO] Creating virtualenv in .venv ...
    py -3 -m venv .venv
)

REM Activate venv
call ".venv\Scripts\activate.bat"

REM Run the intent tap module
echo [RUN] Evaluator Intent Tap
python -m evaluator.intent_tap %*

REM Cleanup
deactivate >nul 2>&1
endlocal
