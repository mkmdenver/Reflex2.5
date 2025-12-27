@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >NUL

REM Runs Trader market-data worker (ticks + quotes).
REM Assumes repo root has .venv and .env already loaded by your main launcher.

set "ROOT=%~dp0..\"
pushd "%ROOT%"

if exist "%ROOT%\.venv\Scripts\python.exe" (
  set "PY=%ROOT%\.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

"%PY%" -m trader.md_worker
popd
endlocal
