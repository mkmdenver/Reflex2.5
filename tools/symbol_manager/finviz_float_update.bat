@echo off
setlocal EnableExtensions

for %%I in ("%~dp0..\..") do set "ROOT=%%~fI"

echo [RUNNING] "%~f0"
echo [PATH] ROOT=%ROOT%

cd /d "%ROOT%"
if errorlevel 1 goto :ERR_ROOT

set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" goto :ERR_PY

set "REQ="
if exist "%ROOT%\requirements-tools.txt" set "REQ=%ROOT%\requirements-tools.txt"
if not defined REQ if exist "%ROOT%\requirements.txt" set "REQ=%ROOT%\requirements.txt"

if defined REQ (
  echo [REQ] Installing "%REQ%" ...
  "%PY%" -m pip install -r "%REQ%"
  if errorlevel 1 goto :ERR_REQ
) else (
  echo [WARN] No requirements file found. Skipping pip install.
)

echo [RUN] %PY% -m tools.symbol_manager.finviz_float_update %*
"%PY%" -m tools.symbol_manager.finviz_float_update %*
exit /b %ERRORLEVEL%

:ERR_ROOT
echo [ERROR] cd failed: "%ROOT%"
exit /b 10

:ERR_PY
echo [ERROR] Missing python: "%PY%"
exit /b 11

:ERR_REQ
echo [ERROR] pip install failed: "%REQ%"
exit /b 12
