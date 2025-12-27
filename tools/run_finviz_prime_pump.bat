@echo off
setlocal ENABLEDELAYEDEXPANSION

REM ============================================================
REM Reflex2 canonical launcher
REM - Enforces root .env
REM - Enforces root .venv
REM - Enforces requirements.txt
REM ============================================================

REM Resolve repo root (tools\..)
set SCRIPT_DIR=%~dp0
set REPO_ROOT=%SCRIPT_DIR%..

REM Normalize
pushd "%REPO_ROOT%" || (
    echo [ERROR] Could not cd to repo root
    exit /b 1
)

echo [PATH] REPO_ROOT=%CD%

REM ------------------------------------------------------------
REM Load root .env (simple KEY=VALUE parser)
REM ------------------------------------------------------------
if not exist ".env" (
    echo [ERROR] .env not found in repo root
    popd
    exit /b 1
)

echo [ENV] Loading .env
for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    REM skip comments and blank lines
    if not "%%A"=="" if not "%%A:~0,1%"=="#" (
        set "%%A=%%B"
    )
)

REM ------------------------------------------------------------
REM Activate venv
REM ------------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Create it first:
    echo         python -m venv .venv
    popd
    exit /b 1
)

set PYTHON=%REPO_ROOT%\.venv\Scripts\python.exe
echo [VENV] Using %PYTHON%

REM ------------------------------------------------------------
REM Ensure requirements are installed
REM ------------------------------------------------------------
if exist "requirements.txt" (
    echo [PIP] Ensuring requirements.txt
    "%PYTHON%" -m pip install --quiet --disable-pip-version-check -r requirements.txt
) else (
    echo [WARN] requirements.txt not found
)

REM ------------------------------------------------------------
REM Run Finviz prime pump
REM ------------------------------------------------------------
echo.
echo [RUN] Finviz Prime Pump
echo ------------------------------------------------------------

"%PYTHON%" tools\finviz_prime_pump.py ^
  --sleep-s 1.2 ^
  add-one MSFT ^
  --tag high_volume_watch ^
  --capture-fundamentals ^
  --pg

set RC=%ERRORLEVEL%

echo.
echo [DONE] exit code=%RC%
echo ------------------------------------------------------------

popd
exit /b %RC%
