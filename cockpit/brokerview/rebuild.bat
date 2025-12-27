@echo off
setlocal enabledelayedexpansion

REM ============================================================
REM Rebuild BrokerView UI (Vite/React) and remove node_modules
REM Run this from: c:\Projects\Reflex2.3\cockpit\brokerview\
REM ============================================================

REM Start from the directory this .bat is in
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"

REM Find the nearest package.json under this tree
set "UI_DIR="

for /f "delims=" %%F in ('dir /b /s "%ROOT%\package.json" 2^>nul') do (
  set "UI_DIR=%%~dpF"
  goto :FOUND
)

:FOUND
if "%UI_DIR%"=="" (
  echo [ERROR] Could not find package.json under: "%ROOT%"
  echo         Expected something like ...\brokerview\templates\package.json
  exit /b 1
)

REM Trim trailing backslash
if "%UI_DIR:~-1%"=="\" set "UI_DIR=%UI_DIR:~0,-1%"

echo [INFO] UI directory: "%UI_DIR%"

REM Ensure npm exists
where npm >nul 2>nul
if errorlevel 1 (
  echo [ERROR] npm not found in PATH. Install Node.js LTS or fix PATH.
  exit /b 2
)

pushd "%UI_DIR%"

echo [STEP] npm install
call npm install
if errorlevel 1 (
  echo [ERROR] npm install failed.
  popd
  exit /b 3
)

echo [STEP] npm run build
call npm run build
if errorlevel 1 (
  echo [ERROR] npm run build failed.
  popd
  exit /b 4
)

echo [STEP] Removing node_modules (optional runtime cleanup)
if exist "%UI_DIR%\node_modules" (
  rmdir /s /q "%UI_DIR%\node_modules" 2>nul
)

popd

echo [OK] BrokerView UI rebuilt successfully.
exit /b 0
