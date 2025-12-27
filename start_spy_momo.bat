@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM Reflex Evaluator - SPY Momo (SimpleBars1 prototype)
REM
REM   Uses evaluator.loop_spy_momo which wraps:
REM     evaluator.models.simple_bars1.SimpleBars1Model
REM ============================================================

set "ROOT=%~dp0"
pushd "%ROOT%"

set "PY=%ROOT%\.venv\Scripts\python.exe"

echo ================== Reflex Evaluator (SPY Momo) ==================
echo   CWD: %ROOT%
echo   Module: evaluator.loop_spy_momo
echo   .env  : %ROOT%\.env
echo ==================================================================
echo.

set "PYTHONPATH=%ROOT%"
"%PY%" -m evaluator.loop_spy_momo %*

popd
endlocal
