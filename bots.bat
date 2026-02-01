@echo off
setlocal

REM ============================================================
REM Reflex2.5 - Start Evaluators (FTS/PTI) + Tap Intents
REM Run this from the Reflex root folder (same folder as env.bat)
REM ============================================================

cd /d "%~dp0"

REM Start bots (from root\evaluator\bots)
start "FTS 1BarUp" cmd /k "cd /d "%~dp0evaluator\bots" && call fts_1barup.bat"
start "FTS RBF"     cmd /k "cd /d "%~dp0evaluator\bots" && call fts_rbf.bat"
start "PTI 1BarUp"  cmd /k "cd /d "%~dp0evaluator\bots" && call pti_1barup.bat"
start "PTI RBF"     cmd /k "cd /d "%~dp0evaluator\bots" && call pti_rbf.bat"

REM Tap intents (from root\tools)
start "Tap Intents" cmd /k "cd /d "%~dp0tools" && call tap_intents.bat"

endlocal
