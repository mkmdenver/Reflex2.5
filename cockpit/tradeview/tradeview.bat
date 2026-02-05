@echo off
setlocal
REM KISS runner for tradeview
call "%~dp0..\..\env.bat" || exit /b 1

set PORT=%TRADEVIEW_PORT%
if "%PORT%"=="" set PORT=7012

echo [RUN] %PY% -m uvicorn tradeview.main:app --host 127.0.0.1 --port %PORT%
%PY% -m uvicorn tradeview.main:app --host 127.0.0.1 --port %PORT%
