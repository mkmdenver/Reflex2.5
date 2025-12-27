@echo off
setlocal ENABLEDELAYEDEXPANSION

set SCRIPT_DIR=%~dp0
set REPO_ROOT=%SCRIPT_DIR%..

pushd "%REPO_ROOT%" || (
  echo [ERROR] Could not cd to repo root
  exit /b 1
)

echo [PATH] REPO_ROOT=%CD%

if not exist ".env" (
  echo [ERROR] .env not found in repo root
  popd & exit /b 1
)

echo [ENV] Loading .env
for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
  if not "%%A"=="" if not "%%A:~0,1%"=="#" set "%%A=%%B"
)

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found in repo root
  popd & exit /b 1
)

set PYTHON=%REPO_ROOT%\.venv\Scripts\python.exe
echo [VENV] Using %PYTHON%

if exist "requirements.txt" (
  echo [PIP] Ensuring requirements.txt
  "%PYTHON%" -m pip install --quiet --disable-pip-version-check -r requirements.txt
)

echo.
echo [RUN] Daily backfill (all available)
echo ------------------------------------------------------------
"%PYTHON%" tools\db_backfill.py --kind daily --symbol ALL --since 1900-01-01 --commit-every 25
set RC=%ERRORLEVEL%
echo ------------------------------------------------------------
echo [DONE] exit code=%RC%

popd
exit /b %RC%
