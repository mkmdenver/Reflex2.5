@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >NUL

REM ------------------------------------------------------------------
REM Resolve repo root (this .bat lives in the repo root)
REM ------------------------------------------------------------------
set "ROOT=%~dp0"
pushd "%ROOT%"

REM --- Activate virtualenv ---
if exist "%ROOT%\.venv\Scripts\activate.bat" (
  call "%ROOT%\.venv\Scripts\activate.bat"
) else (
  echo [ERROR] venv not found at %ROOT%\.venv
  popd
  exit /b 1
)

set "PY=%ROOT%\.venv\Scripts\python.exe"
set "PYTHONPATH=%ROOT%"