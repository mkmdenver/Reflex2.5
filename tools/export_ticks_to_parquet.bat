@echo off
setlocal ENABLEDELAYEDEXPANSION

REM ============================================================
REM Reflex Tool Runner Template (standard)
REM - Loads ROOT .env then .env.local into environment
REM - Uses tool-local venv: <this folder>\.venv
REM - Installs deps from repo-root requirements.txt
REM - Runs from repo root for stable imports
REM
REM EDIT ONLY: SCRIPT_REL
REM ============================================================

REM --- EDIT THIS ONE LINE ONLY ---
set "SCRIPT_REL=tools\export_ticks_to_parquet.py"
REM e.g. set "SCRIPT_REL=evaluator\abots\pts_ross_bull_flag.py"

set "TOOL_DIR=%~dp0"

REM Repo root guess: 1-up, else 2-up
for %%I in ("%TOOL_DIR%..") do set "ROOT_DIR=%%~fI"
if not exist "%ROOT_DIR%\requirements.txt" (
  for %%I in ("%TOOL_DIR%..\..") do set "ROOT_DIR=%%~fI"
)

set "VENV_DIR=%TOOL_DIR%.venv"
set "PY=%VENV_DIR%\Scripts\python.exe"
set "PIP=%VENV_DIR%\Scripts\python.exe -m pip"
set "REQ=%ROOT_DIR%\requirements.txt"
set "SCRIPT=%ROOT_DIR%\%SCRIPT_REL%"

echo [PATH] ROOT_DIR=%ROOT_DIR%
echo [PATH] TOOL_DIR=%TOOL_DIR%
echo [PATH] VENV_DIR=%VENV_DIR%
echo [PATH] SCRIPT=%SCRIPT_REL%

if not exist "%REQ%" (
  echo [ERROR] requirements.txt not found at: "%REQ%"
  exit /b 2
)

if not exist "%SCRIPT%" (
  echo [ERROR] Script not found: "%SCRIPT%"
  exit /b 6
)

REM ----- Load .env + .env.local (ROOT) -----
call :load_env_file "%ROOT_DIR%\.env"
call :load_env_file "%ROOT_DIR%\.env.local"

REM Create venv if missing
if not exist "%PY%" (
  echo [VENV] Creating virtualenv at "%VENV_DIR%" ...
  py -3 -m venv "%VENV_DIR%"
  if errorlevel 1 (
    echo [ERROR] Failed creating venv.
    exit /b 3
  )
)

call "%VENV_DIR%\Scripts\activate.bat"

echo [PIP] Installing repo requirements from "%REQ%" ...
%PIP% install -r "%REQ%"
if errorlevel 1 (
  echo [ERROR] requirements install failed.
  exit /b 5
)

pushd "%ROOT_DIR%" >nul
echo [RUN] %PY% %SCRIPT_REL% %*
%PY% "%SCRIPT%" %*
set "EC=%ERRORLEVEL%"
popd >nul

echo [DONE] Exit code %EC%
exit /b %EC%

REM ------------------------------------------------------------
REM Loads KEY=VALUE lines into environment.
REM Skips blank lines and lines starting with # or ;
REM Supports "export KEY=VALUE"
REM Strips surrounding quotes from VALUE if present.
REM ------------------------------------------------------------
:load_env_file
set "ENV_FILE=%~1"
if not exist "%ENV_FILE%" goto :eof

echo [ENV] Loading %ENV_FILE%

for /f "usebackq delims=" %%A in ("%ENV_FILE%") do (
  set "LINE=%%A"

  REM trim leading spaces (cheap)
  for /f "tokens=* delims= " %%B in ("!LINE!") do set "LINE=%%B"

  if "!LINE!"=="" (
    REM skip
  ) else if "!LINE:~0,1!"=="#" (
    REM skip
  ) else if "!LINE:~0,1!"==";" (
    REM skip
  ) else (
    if /i "!LINE:~0,7!"=="export " set "LINE=!LINE:~7!"

    for /f "tokens=1* delims==" %%K in ("!LINE!") do (
      set "K=%%K"
      set "V=%%L"

      REM strip surrounding quotes
      if "!V:~0,1!"=="^"" if "!V:~-1!"=="^"" set "V=!V:~1,-1!"

      set "!K!=!V!"
    )
  )
)
goto :eof
