@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >NUL

REM Publish PTI-like intents into Redis for Trader testing.
REM Example:
REM   pti_intent_gen.bat paper1 SPY buy 50
REM   pti_intent_gen.bat paper1 SPY buy notional 25

set "ROOT=%~dp0..\"
pushd "%ROOT%"

if exist "%ROOT%\.venv\Scripts\python.exe" (
  set "PY=%ROOT%\.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

set "ACCOUNT=%~1"
set "SYMBOL=%~2"
set "SIDE=%~3"
set "ARG4=%~4"
set "ARG5=%~5"

if "%ACCOUNT%"=="" (
  echo Usage: pti_intent_gen.bat ACCOUNT_ID SYMBOL SIDE [qty ^| notional AMOUNT]
  exit /b 1
)

if "%SYMBOL%"=="" set "SYMBOL=SPY"
if "%SIDE%"=="" set "SIDE=buy"

if /I "%ARG4%"=="notional" (
  if "%ARG5%"=="" (
    echo Missing notional amount.
    exit /b 1
  )
  "%PY%" -m trader.pti_intent_gen --account-id "%ACCOUNT%" --symbol "%SYMBOL%" --side "%SIDE%" --notional %ARG5%
) else (
  if "%ARG4%"=="" set "ARG4=1"
  "%PY%" -m trader.pti_intent_gen --account-id "%ACCOUNT%" --symbol "%SYMBOL%" --side "%SIDE%" --qty %ARG4%
)

popd
endlocal
