@echo off
setlocal
REM KISS runner for engineerview
call "%~dp0..\..\env.bat" || exit /b 1

set PORT=%ENGINEERVIEW_PORT%
if "%PORT%"=="" set PORT=7013

echo [RUN] %PY% -m uvicorn engineerview.main:app --host 127.0.0.1 --port %PORT%
%PY% -m uvicorn engineerview.main:app --host 127.0.0.1 --port %PORT%
