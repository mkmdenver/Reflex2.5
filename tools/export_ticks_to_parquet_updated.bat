@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ------------------------------------------------------------
REM Export ticks from Postgres -> Parquet tick lake
REM
REM This .bat lives in tools\ alongside the .py.
REM It always RUNS from repo root so imports/.env behave correctly.
REM Virtualenv is kept in tools\.venv (portable, script-local).
REM ------------------------------------------------------------

REM Directory of this .bat (tools\) with trailing backslash
set "TOOLS_DIR=%~dp0"

REM ROOT_DIR = parent of tools\
for %%I in ("%TOOLS_DIR%..") do set "ROOT_DIR=%%~fI"

REM Virtualenv in tools\.venv
set "VENV_DIR=%TOOLS_DIR%.venv"

if not exist "%VENV_DIR%\Scripts\python.exe" (
  echo [VENV] Creating virtualenv at %VENV_DIR% ...
  py -3 -m venv "%VENV_DIR%"
)

call "%VENV_DIR%\Scripts\activate.bat"

REM Optional one-time install; uncomment if needed:
REM pip install psycopg[binary] pyarrow python-dotenv

pushd "%ROOT_DIR%"

echo [RUN] python tools\export_ticks_to_parquet.py %*
python tools\export_ticks_to_parquet.py %*
set "EC=%ERRORLEVEL%"

popd

echo [DONE] Exit code %EC%
endlocal & exit /b %EC%
