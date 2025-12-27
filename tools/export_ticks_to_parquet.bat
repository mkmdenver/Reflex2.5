@echo off
setlocal ENABLEDELAYEDEXPANSION

REM ============================================================
REM export_ticks_to_parquet - launcher that runs from tools\
REM but installs dependencies from repo-root requirements.txt
REM ============================================================

REM Where this .bat lives (tools\)
set "TOOL_DIR=%~dp0"
REM Repo root is one level up from tools\
for %%I in ("%TOOL_DIR%..") do set "ROOT_DIR=%%~fI"

set "VENV_DIR=%TOOL_DIR%.venv"
set "PY=%VENV_DIR%\Scripts\python.exe"
set "PIP=%VENV_DIR%\Scripts\python.exe -m pip"
set "REQ=%ROOT_DIR%\requirements.txt"

if not exist "%REQ%" (
  echo [ERROR] requirements.txt not found at: "%REQ%"
  echo         Expected it in repo root.
  exit /b 2
)

REM Create venv if missing
if not exist "%PY%" (
  echo [VENV] Creating virtualenv at "%VENV_DIR%" ...
  py -3 -m venv "%VENV_DIR%"
  if errorlevel 1 (
    echo [ERROR] Failed creating venv.
    exit /b 3
  )
)

REM Activate + install deps (root requirements)
call "%VENV_DIR%\Scripts\activate.bat"

echo [PIP] Upgrading pip/setuptools/wheel ...
%PIP% install -U pip setuptools wheel
if errorlevel 1 (
  echo [ERROR] pip upgrade failed.
  exit /b 4
)

echo [PIP] Installing repo requirements from "%REQ%" ...
%PIP% install -r "%REQ%"
if errorlevel 1 (
  echo [ERROR] requirements install failed.
  exit /b 5
)

REM Run from repo root so imports that expect root work
pushd "%ROOT_DIR%" >nul

echo [RUN] %PY% tools\export_ticks_to_parquet.py %*
%PY% tools\export_ticks_to_parquet.py %*
set "EC=%ERRORLEVEL%"

popd >nul
echo [DONE] Exit code %EC%
exit /b %EC%
