@echo off
setlocal enabledelayedexpansion

REM -----------------------------------------------------------------------------
REM pti_probe.bat - simple "what is flowing?" console for bars/ticks + FTS stream
REM Run from: c:\Projects\Reflex2.3\evaluator\bots
REM -----------------------------------------------------------------------------

set "ROOT=%~dp0..\.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"

echo [PATH] ROOT=%ROOT%

REM Load env like your other bats do
if exist "%ROOT%\.env" (
  echo [ENV] Loading .env from %ROOT%\.env
  for /f "usebackq delims=" %%A in ("%ROOT%\.env") do (
    set "LINE=%%A"
    if not "!LINE!"=="" if "!LINE:~0,1!" NEQ "#" (
      for /f "tokens=1,* delims==" %%K in ("!LINE!") do (
        if not "%%K"=="" if not defined %%K set "%%K=%%L"
      )
    )
  )
)

set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo [RUN] %PY% "%ROOT%\evaluator\bots\pti_probe.py" --env "%ROOT%\.env"
%PY% "%ROOT%\evaluator\bots\pti_probe.py" --env "%ROOT%\.env" %*

endlocal
