@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM pts_template.bat
REM
REM Wrapper for evaluator.abots.pts_template
REM
REM Usage:
REM    pts_template SYMBOL_OR_PATTERN START [END]
REM
REM Examples:
REM    pts_template KROS 2025-08-01
REM    pts_template KROS 2025-08-01 2025-12-05
REM    pts_template ALL  2025-11-25
REM    pts_template K*   2025-08-01 2025-08-31
REM
REM SYMBOL_OR_PATTERN:
REM    KROS  -> single symbol
REM    ALL   -> all symbols found under parquet_root
REM    K*    -> wildcard against symbol dirs
REM
REM This script:
REM   - Creates/uses a .venv in this abots directory
REM   - Runs python -m evaluator.bots.abots.eval_shape_range
REM     from the project root so imports work.
REM ============================================================

if "%~1"=="" goto :usage
if "%~2"=="" goto :usage

set "ABOTS_DIR=%~dp0"
for %%I in ("%ABOTS_DIR%\..\..\..") do set "REPO_ROOT=%%~fI"

echo [PATH] ABOTS_DIR=%ABOTS_DIR%
echo [PATH] REPO_ROOT=%REPO_ROOT%

REM ------------------------------------------------------------
REM Ensure virtualenv in abots dir
REM ------------------------------------------------------------
set "VENV_DIR=%ABOTS_DIR%\.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [VENV] Creating virtualenv at %VENV_DIR% ...
    py -3 -m venv "%VENV_DIR%"
)

echo [VENV] Using %VENV_PY%

REM ------------------------------------------------------------
REM Run module from repo root so evaluator.* imports work
REM ------------------------------------------------------------
pushd "%REPO_ROOT%"

set "ARG1=%~1"
set "ARG2=%~2"
set "ARG3=%~3"

if "%ARG3%"=="" (
    "%VENV_PY%" -m evaluator.abots.pts_template "%ARG1%" "%ARG2%"
) else (
    "%VENV_PY%" -m evaluator.abots.pts_template "%ARG1%" "%ARG2%" "%ARG3%"
)

set "EXITCODE=%ERRORLEVEL%"
popd

exit /b %EXITCODE%

:usage
echo Usage:
echo   eval_shape_range SYMBOL_OR_PATTERN START [END]
echo.
echo Examples:
echo   eval_shape_range KROS 2025-08-01
echo   eval_shape_range KROS 2025-08-01 2025-12-05
echo   eval_shape_range ALL  2025-11-25
echo   eval_shape_range K*   2025-08-01 2025-08-31
exit /b 1
