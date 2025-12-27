@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM Reflex2.3 - Simple stack launcher
REM   - Run from project ROOT (c:\Projects\Reflex2.3)
REM   - Starts:
REM       * datahub.bat       (DataHub + worker)
REM       * eval.bat          (evaluator backend services)
REM       * evalview.bat      (EvalView cockpit)
REM       * cockpit_control   (placeholder - fill in your .bat)
REM       * intent_tap.bat    (general intent tap)
REM       * fts_simple_bot.bat (simple filter)
REM       * pti_simple_bot.bat (simple pattern/intent)
REM   - All consoles are started minimized.
REM ============================================================

REM Resolve ROOT to the directory where this script lives
set "ROOT=%~dp0"
REM Strip trailing backslash if present
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

echo [PATH] ROOT = %ROOT%
cd /d "%ROOT%"

REM ------------------------------------------------------------
REM 1) Start DataHub stack
REM ------------------------------------------------------------
if exist "%ROOT%\datahub.bat" (
    echo [START] DataHub...
    start "Reflex DataHub" /min cmd /c "%ROOT%\datahub.bat"
) else (
    echo [WARN] datahub.bat not found in %ROOT%  (adjust simple.bat if name/path differs)
)

REM ------------------------------------------------------------
REM 2) Start Evaluator backend stack
REM ------------------------------------------------------------
if exist "%ROOT%\eval.bat" (
    echo [START] Evaluator services...
    start "Reflex Evaluator" /min cmd /c "%ROOT%\eval.bat"
) else (
    echo [WARN] eval.bat not found in %ROOT%  (adjust simple.bat if name/path differs)
)

REM ------------------------------------------------------------
REM 3) Start EvalView cockpit
REM ------------------------------------------------------------
if exist "%ROOT%\evalview.bat" (
    echo [START] EvalView cockpit...
    start "EvalView" /min cmd /c "%ROOT%\evalview.bat"
) else (
    echo [WARN] evalview.bat not found in %ROOT%  (adjust simple.bat if name/path differs)
)

REM ------------------------------------------------------------
REM 4) Start Cockpit control (trader / control UI)
REM     NOTE: replace cockpit_control.bat with your actual launcher
REM ------------------------------------------------------------
if exist "%ROOT%\cockpit_control.bat" (
    echo [START] Cockpit control...
    start "CockpitControl" /min cmd /c "%ROOT%\cockpit_control.bat"
) else (
    echo [INFO] cockpit_control.bat not found in %ROOT%  (hook left for your control cockpit)
)

REM ------------------------------------------------------------
REM 5) Start INTENT TAP (debug listener)
REM ------------------------------------------------------------
if exist "%ROOT%\evaluator\bots\intent_tap.bat" (
    echo [START] intent_tap...
    pushd "%ROOT%\evaluator\bots"
    start "IntentTap" /min cmd /c intent_tap.bat
    popd
) else (
    echo [WARN] intent_tap.bat not found in %ROOT%\evaluator\bots
)

REM ------------------------------------------------------------
REM 6) Start SIMPLE FILTER bot (FTS_simple1)
REM ------------------------------------------------------------
if exist "%ROOT%\evaluator\bots\fts_simple_bot.bat" (
    echo [START] FTS_simple1 bot...
    pushd "%ROOT%\evaluator\bots"
    start "FTS_simple1" /min cmd /c fts_simple_bot.bat
    popd
) else (
    echo [WARN] fts_simple_bot.bat not found in %ROOT%\evaluator\bots
)

REM ------------------------------------------------------------
REM 7) Start SIMPLE PATTERN/INTENT bot (PTI_simple1)
REM ------------------------------------------------------------
if exist "%ROOT%\evaluator\bots\pti_simple_bot.bat" (
    echo [START] PTI_simple1 bot...
    pushd "%ROOT%\evaluator\bots"
    start "PTI_simple1" /min cmd /c pti_simple_bot.bat
    popd
) else (
    echo [WARN] pti_simple_bot.bat not found in %ROOT%\evaluator\bots
)

echo.
echo [DONE] Simple Reflex stack launch commands issued.
echo       Check minimized windows in the taskbar for logs.
echo.

endlocal
