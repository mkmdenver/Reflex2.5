@echo off
setlocal
REM KISS runner for modelview
call "%~dp0..\..\env.bat" || exit /b 1

set PORT=%MODELVIEW_PORT%
if "%PORT%"=="" set PORT=7011

echo [RUN] %PY% -m uvicorn modelview.main:app --host 127.0.0.1 --port %PORT%
%PY% -m uvicorn modelview.main:app --host 127.0.0.1 --port %PORT%
