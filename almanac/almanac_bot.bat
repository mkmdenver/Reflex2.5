@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM almanac_bot.bat
REM - Lives in /almanac
REM - Uses root .env and root requirements.txt
REM ============================================================

set "HERE=%~dp0"
for %%I in ("%HERE%\..") do set "REPO_ROOT=%%~fI"

echo [PATH] HERE=%HERE%
echo [PATH] REPO_ROOT=%REPO_ROOT%

set "VENV_DIR=%HERE%\.venv"
set "PY=%VENV_DIR%\Scripts\python.exe"

if not exist "%PY%" (
  echo [VENV] Creating %VENV_DIR% ...
  py -3 -m venv "%VENV_DIR%"
)

echo [VENV] Installing root requirements...
"%PY%" -m pip install --upgrade pip >nul
"%PY%" -m pip install -r "%REPO_ROOT%\requirements.txt"

pushd "%HERE%"
"%PY%" "almanac_bot.py"
set "EC=%ERRORLEVEL%"
popd

exit /b %EC%
