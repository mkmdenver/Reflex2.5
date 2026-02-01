@echo off
setlocal enabledelayedexpansion

REM This .bat lives in ROOT\evaluator\abots
REM Go to project ROOT
set SCRIPT_DIR=%~dp0
pushd "%SCRIPT_DIR%\..\.."

echo [PATH] ROOT_DIR=%CD%

REM ---- LOAD .env MANUALLY ----
if exist ".env" (
    echo [ENV] Loading .env from %CD%\.env
    for /f "usebackq tokens=1,2 delims==;" %%A in (.env) do (
        set KEY=%%A
        set VAL=%%B
        if defined KEY if not "!KEY!"=="" (
            REM Strip possible quotes around value
            set VAL=!VAL:"=!
            set !KEY!=!VAL!
        )
    )
) else (
    echo [ENV] WARNING: no .env found
)


REM ---- LOAD .env.local (overrides) ----
if exist ".env.local" (
    echo [ENV] Loading .env.local from %CD%\.env.local
    for /f "usebackq tokens=1,2 delims==;" %%A in (.env.local) do (
        set KEY=%%A
        set VAL=%%B
        if defined KEY if not "!KEY!"=="" (
            set VAL=!VAL:"=!
            set !KEY!=!VAL!
        )
    )
)

REM ---- ACTIVATE VENV ----
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Cannot find root venv
    popd
    exit /b 1
)
call ".venv\Scripts\activate.bat"

echo [RUN] pts_ross_bull_flag.py %*

python "%SCRIPT_DIR%\pts_ross_bull_flag.py" %*

popd
endlocal
