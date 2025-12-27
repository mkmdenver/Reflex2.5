@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM Reflex2 - Start Evaluator MCP Orchestrator
REM
REM This process:
REM   - Runs RossPillarsFilter (stage 1) against hub ticks
REM   - Runs PatternMatcher (stage 2) against eval.ross_pillars
REM   - Publishes EvalState telemetry on eval.state (for EvalView)
REM   - Optionally emits order intents on eval.order_intent
REM
REM Usage:
REM   start_eval_mcp
REM   start_eval_mcp evalA
REM   start_eval_mcp evalB
REM
REM Arguments:
REM   %1  (optional) EVAL_ID (default: evalA)
REM
REM Env toggles:
REM   MCP_GENERATE_INTENTS=0/1  (default: 0)
REM ============================================================

REM Figure out repo root (directory of this .bat)
set "REPO_ROOT=%~dp0"
REM Strip trailing backslash if present
if "%REPO_ROOT:~-1%"=="\" set "REPO_ROOT=%REPO_ROOT:~0,-1%"

echo [PATH] REPO_ROOT = %REPO_ROOT%

cd /d "%REPO_ROOT%"

REM ------------------------------------------------------------
REM Virtualenv
REM ------------------------------------------------------------
if not exist ".venv" (
    echo [VENV] Creating virtualenv in .venv ...
    python -m venv .venv
)

if exist ".venv\Scripts\activate.bat" (
    echo [VENV] Activating virtualenv ...
    call ".venv\Scripts\activate.bat"
) else (
    echo [VENV] ERROR: Could not find .venv\Scripts\activate.bat
    goto :EOF
)

REM ------------------------------------------------------------
REM Install/refresh dependencies from requirements.txt
REM (idempotent: pip will skip already-satisfied packages)
REM ------------------------------------------------------------
if exist "requirements.txt" (
    echo [PIP] Installing dependencies from requirements.txt ...
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
) else (
    echo [PIP] WARNING: requirements.txt not found in %REPO_ROOT%
)

REM ------------------------------------------------------------
REM EVAL_ID (from arg or default)
REM ------------------------------------------------------------
set "EVAL_ID=%~1"
if "%EVAL_ID%"=="" set "EVAL_ID=evalA"
set "EVAL_INSTANCE=%EVAL_ID%"

REM Default: do NOT generate real order intents unless explicitly enabled
if "%MCP_GENERATE_INTENTS%"=="" set "MCP_GENERATE_INTENTS=0"

echo [ENV] EVAL_ID=%EVAL_ID%
echo [ENV] EVAL_INSTANCE=%EVAL_INSTANCE%
echo [ENV] MCP_GENERATE_INTENTS=%MCP_GENERATE_INTENTS%

REM ------------------------------------------------------------
REM Launch MCP orchestrator
REM ------------------------------------------------------------
echo [RUN] Starting MCP orchestrator (evaluator.mcp_orchestrator) ...
echo [RUN]   Eval ID: %EVAL_ID%
echo [RUN]   Intents: %MCP_GENERATE_INTENTS%

python -m evaluator.mcp_orchestrator

REM Keep window open if user started by double-clicking
echo.
echo [DONE] MCP orchestrator exited.
pause

endlocal
