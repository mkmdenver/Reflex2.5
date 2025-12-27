@echo off
setlocal ENABLEDELAYEDEXPANSION

rem ------------------------------------------------------------
rem Usage: scripts\Start-ReflexPair.bat .env.liveA
rem Optional: set FAST_START=1 to skip pip install -r requirements.txt
rem ------------------------------------------------------------

set "ENVFILE=%~1"
if "%ENVFILE%"=="" (
  echo Usage: scripts\Start-ReflexPair.bat path\to\.env
  exit /b 1
)

rem --- Load .env into this CMD session (comments/# and blank lines ignored)
if not exist "%ENVFILE%" (
  echo [setup] ERROR: env file not found: %ENVFILE%
  exit /b 1
)
for /f "usebackq tokens=1,* delims== eol=#" %%A in ("%ENVFILE%") do (
  set "%%~A=%%~B"
)

set "ROOT=%CD%"
set "VENV=%ROOT%\.venv"
set "PY=%VENV%\Scripts\python.exe"

where python >nul 2>&1
if errorlevel 1 (
  echo [setup] ERROR: No "python" on PATH. Install Python 3.10+ and retry.
  pause
  exit /b 1
)

if not exist "%PY%" (
  echo [setup] Creating virtualenv at %VENV% ...
  python -m venv .venv
  if errorlevel 1 (
    echo [setup] ERROR creating venv.
    pause
    exit /b 1
  )
)
rem --- Ensure pip exists inside the venv ---
set "PIP=%VENV%\Scripts\pip.exe"
if not exist "%PIP%" (
  echo [setup] pip not found in venv -> bootstrapping with ensurepip ...
  "%PY%" -m ensurepip --upgrade
)

rem If pip still not there, try venv upgrade-deps (Python 3.9+)
if not exist "%PIP%" (
  echo [setup] ensurepip failed; trying venv --upgrade-deps ...
  rmdir /s /q "%VENV%" 2>nul
  python -m venv --upgrade-deps .venv
  set "PY=%VENV%\Scripts\python.exe"
  set "PIP=%VENV%\Scripts\pip.exe"
)

if not exist "%PIP%" (
  echo [setup] ERROR: pip still missing. Your Python install may lack ensurepip.
  echo         Reinstall Python with "pip" included, then rerun.
  pause
  exit /b 1
)

rem Upgrade pip quietly
"%PY%" -m pip install --upgrade pip >nul 2>nul

rem Install requirements unless FAST_START=1
if exist "%ROOT%\requirements.txt" (
  if /I "%FAST_START%"=="1" (
    echo [setup] FAST_START=1 -> skipping pip install -r requirements.txt
  ) else (
    echo [setup] Installing dependencies from requirements.txt ...
    "%PY%" -m pip install -r "%ROOT%\requirements.txt"
    if errorlevel 1 (
      echo [setup] ERROR installing requirements.
      pause
      exit /b 1
    )
  )
) else (
  echo [setup] WARNING: requirements.txt not found; continuing...
)

if "%REFLEX__SERVER__PORT%"=="" set "REFLEX__SERVER__PORT=7000"
set "HUB_PORT=%REFLEX__SERVER__PORT%"
if "%EVAL_SYMBOL%"=="" set "EVAL_SYMBOL=AAPL"
set "HUB_URL=http://127.0.0.1:%HUB_PORT%"

set "HUB_TITLE=DataHub %REFLEX__INSTANCE_ID% :%HUB_PORT%"
set "EVAL_TITLE=Evaluator %REFLEX__INSTANCE_ID%"

rem --- Start DataHub (visible console; pauses if it errors) ---
start "%HUB_TITLE%" cmd /c "%PY% -m datahub.worker || (echo.& echo [DataHub] exited with error. Press any key... & pause)"

rem Give hub a moment
ping -n 3 127.0.0.1 >nul

rem --- Start Evaluator (visible console; keeps window if it errors) ---
start "%EVAL_TITLE%" cmd /c "set HUB_URL=%HUB_URL% && set EVAL_SYMBOL=%EVAL_SYMBOL% && %PY% -m evaluator.service || (echo.& echo [Evaluator] exited with error. Press any key... & pause)"

echo Launched. Health: http://localhost:%HUB_PORT%/health
endlocal
exit /b 0
