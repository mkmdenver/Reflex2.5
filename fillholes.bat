REM @echo off
setlocal ENABLEDELAYEDEXPANSION

REM ------------------------------------------------------------
REM fillholes.bat
REM
REM Run from repo root. Example:
REM   fillholes --since 2025-07-28 --until 2025-11-07 --what both
REM   fillholes --since 2025-07-28 --until 2025-11-07 --what minute --dry-run
REM ------------------------------------------------------------

REM Resolve repo root as the directory of this .bat
set "REPO_ROOT=%~dp0"

REM Normalize to root (in case it’s called from elsewhere)
cd /d "%REPO_ROOT%"

REM Where we expect the venv Python to live
set "VENV_PY=%REPO_ROOT%.venv\Scripts\python.exe"

IF NOT EXIST "%VENV_PY%" (
    echo [SETUP] Creating virtualenv in .venv
    py -3 -m venv .venv
    IF ERRORLEVEL 1 (
        echo [ERROR] Failed to create virtualenv
        goto :EOF
    )

    echo [SETUP] Upgrading pip and installing requirements.txt
    "%VENV_PY%" -m pip install --upgrade pip
    IF EXIST "%REPO_ROOT%requirements.txt" (
        "%VENV_PY%" -m pip install -r "%REPO_ROOT%requirements.txt"
    ) ELSE (
        echo [WARN] requirements.txt not found at %REPO_ROOT%requirements.txt
    )
)

REM Make sure repo root is on PYTHONPATH so packages resolve cleanly
set "PYTHONPATH=%REPO_ROOT%"

REM -----------------------------------------------------------------
REM Load DSN from .env:
REM   1) REFLEX__PG_DSN if present
REM   2) otherwise REFLEX_PG_DSN
REM and export it as REFLEX__PG_DSN for the Python script.
REM -----------------------------------------------------------------
IF NOT DEFINED REFLEX__PG_DSN (
    IF EXIST "%REPO_ROOT%.env" (
        for /f "usebackq tokens=1,* delims==" %%A in ("%REPO_ROOT%.env") do (
            if /I "%%A"=="REFLEX__PG_DSN" (
                set "REFLEX__PG_DSN=%%B"
            ) else if /I "%%A"=="REFLEX_PG_DSN" (
                set "REFLEX__PG_DSN=%%B"
            )
        )
    )
)

IF NOT DEFINED REFLEX__PG_DSN (
    echo [ERROR] REFLEX__PG_DSN / REFLEX_PG_DSN not set in environment or .env
    echo         Please add a line like:
    echo           REFLEX_PG_DSN=postgresql://user:pass@host:5432/stock_data
    goto :EOF
)

echo [INFO] Using REFLEX__PG_DSN=%REFLEX__PG_DSN%

REM -----------------------------------------------------------------
REM Call the gap-filler module
REM -----------------------------------------------------------------
echo [RUN] tools.symbol_manager.db_fill_holes %*
"%VENV_PY%" -m tools.symbol_manager.db_fill_holes %*

endlocal
