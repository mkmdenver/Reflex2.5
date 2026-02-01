@echo off
setlocal EnableExtensions

REM ------------------------------------------------------------
REM tools\tap_bars.bat
REM Lives in tools\
REM KISS: resolve repo ROOT, load .env + .env.local, run venv python.
REM ------------------------------------------------------------

REM This BAT is in tools\, so repo root is one directory up.
set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"

echo [RUNNING] "%~f0"
echo [PATH] ROOT=%ROOT%

pushd "%ROOT%"

if not exist "%ROOT%\env.bat" (
  echo [ERROR] Missing "%ROOT%\env.bat"
  popd
  exit /b 1
)

if not exist "%ROOT%\.env" (
  echo [ERROR] Missing "%ROOT%\.env"
  popd
  exit /b 1
)

echo [ENV] Loading .env from %ROOT%\.env ...
call "%ROOT%\env.bat" "%ROOT%\.env"
if errorlevel 1 (
  echo [ERROR] env.bat failed on "%ROOT%\.env"
  popd
  exit /b 1
)

if exist "%ROOT%\.env.local" (
  echo [ENV] Loading .env.local from %ROOT%\.env.local ...
  call "%ROOT%\env.bat" "%ROOT%\.env.local"
  if errorlevel 1 (
    echo [ERROR] env.bat failed on "%ROOT%\.env.local"
    popd
    exit /b 1
  )
)

set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] Missing venv python: "%PY%"
  popd
  exit /b 2
)

set "REFLEX_MODE=LIVE"

echo %GARNET_URL%
echo %REDIS_URL%
echo %REFLEX_MODE%

echo [RUN] %PY% tools\tap_ticks.py %*
"%PY%" "tools\tap_ticks.py" %*
set "RC=%ERRORLEVEL%"

popd
exit /b %RC%
