@echo off
setlocal ENABLEDELAYEDEXPANSION

REM === CONFIG ===
set "REFLEX_ROOT=C:\Projects\Reflex2.3"

if "%~1"=="" (
    echo Usage: %~n0 YYYY-MM-DD
    exit /b 1
)

set "SINCE=%~1"

REM ------------------------------------------------------------
REM Resolve repo root (this .bat's directory)
REM ------------------------------------------------------------
set "REPO_ROOT=%~dp0"
cd /d "%REPO_ROOT%"

echo [PATH] REPO_ROOT = %REPO_ROOT%

REM ------------------------------------------------------------
REM Load .env into process environment
REM   - skip empty lines
REM   - skip lines starting with #
REM   - supports KEY=VALUE (VALUE may contain '=')
REM ------------------------------------------------------------
if exist ".env" (
    echo [ENV] Loading .env ...
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        set "line=%%A"
        if not "!line!"=="" (
            if "!line:~0,1!" NEQ "#" (
                set "%%A=%%B"
            )
        )
    )
) else (
    echo [WARN] .env not found in %REPO_ROOT%
)

REM ------------------------------------------------------------
REM Ensure virtualenv + requirements
REM ------------------------------------------------------------
set "VENV_DIR=%REPO_ROOT%.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo [VENV] Creating virtualenv at %VENV_DIR% ...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Failed to create virtualenv.
        exit /b 1
    )

    echo [VENV] Upgrading pip...
    "%PYTHON_EXE%" -m pip install --upgrade pip
    if errorlevel 1 (
        echo [ERROR] Failed to upgrade pip.
        exit /b 1
    )

    if exist "requirements.txt" (
        echo [VENV] Installing requirements from requirements.txt ...
        "%PYTHON_EXE%" -m pip install -r requirements.txt
        if errorlevel 1 (
            echo [ERROR] Failed to install requirements.
            exit /b 1
        )
    ) else (
        echo [WARN] requirements.txt not found, skipping pip install.
    )
) else (
    echo [VENV] Using existing virtualenv at %VENV_DIR%.
)

set "PY=%PYTHON_EXE%"
set "MODULE=tools.symbol_manager.db_backfill"


echo.
echo [RUN] TICK backfill ALL since %SINCE%
"%PY%" -m %MODULE% --kind tick --symbol ALL --since %SINCE%
if errorlevel 1 (
    echo [ERROR] TICK backfill failed with exit code %errorlevel%.
    exit /b %errorlevel%
)



endlocal
