@echo off
setlocal EnableExtensions EnableDelayedExpansion
set "ROOT=%~dp0"
pushd "%ROOT%"

if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv || (echo [ERROR] failed to create venv & popd & exit /b 1)
)

set "PY=%ROOT%\.venv\Scripts\python.exe"
set "PIP=%ROOT%\.venv\Scripts\pip.exe"
"%PIP%" install --upgrade pip
if exist "%ROOT%\requirements.txt" (
  "%PIP%" install -r requirements.txt || (echo [ERROR] pip install failed & popd & exit /b 1)
)

echo [OK] venv ready.
popd
exit /b 0
