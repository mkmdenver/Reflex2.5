@echo off
setlocal ENABLEDELAYEDEXPANSION

REM tools\replay_bars.bat
REM KISS: tool lives under tools\ but loads ROOT\.env and ROOT\.env.local
REM ROOT = parent of this BAT directory

set "BATDIR=%~dp0"
for %%I in ("%BATDIR%..") do set "ROOT=%%~fI"
if not "%ROOT:~-1%"=="\" set "ROOT=%ROOT%\"

set "PY=%ROOT%\.venv\Scripts\python.exe"
set "SCRIPT=%ROOT%tools\replay_bars_main.py"

if not exist "%ROOT%\.env" (
  echo [ERROR] .env not found in "%ROOT%"
  exit /b 1
)

if not exist "%PY%" (
  echo [ERROR] Missing venv python: "%PY%"
  exit /b 1
)

if not exist "%SCRIPT%" (
  echo [ERROR] Missing script: "%SCRIPT%"
  exit /b 1
)

REM --- load .env ---
for /f "usebackq tokens=1,* delims==" %%A in ("%ROOT%\.env") do (
  set "K=%%A"
  set "V=%%B"
  if not "!K!"=="" (
    if not "!K:~0,1!"=="#" (
      if not defined !K! set "!K!=!V!"
    )
  )
)

REM --- load .env.local (optional) ---
if exist "%ROOT%\.env.local" (
  for /f "usebackq tokens=1,* delims==" %%A in ("%ROOT%\.env.local") do (
    set "K=%%A"
    set "V=%%B"
    if not "!K!"=="" (
      if not "!K:~0,1!"=="#" (
        if not defined !K! set "!K!=!V!"
      )
    )
  )
)

echo [RUNNING] tools\replay_bars
echo [ENV] ROOT=%ROOT%
echo [ENV] PY=%PY%
echo [ENV] SCRIPT=%SCRIPT%

pushd "%ROOT%" >nul
"%PY%" "%SCRIPT%" %*
set "RC=%ERRORLEVEL%"
popd >nul
exit /b %RC%
