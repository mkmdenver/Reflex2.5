@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >NUL

REM intent_replayer.bat
REM Root-runnable. Uses project venv if present, otherwise falls back to python on PATH.
REM Loads .env + .env.local inside Python via trader.envload (canonical).

set "ROOT=%~dp0"
pushd "%ROOT%"

if exist "%ROOT%\.venv\Scripts\python.exe" (
  set "PY=%ROOT%\.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

if "%~1"=="" (
  echo Usage:
  echo   intent_replay.bat ^<intents.jsonl^> [--instance replayA] [--max 0] [--skip 0] [--dry-run]
  echo Examples:
  echo   intent_replay.bat data\intents_20260111_120001.jsonl --instance replayA
  echo   intent_replay.bat data\intents_20260111_120001.jsonl --instance replayA --dry-run
  exit /b 1
)

"%PY%" -m tools.INTENT_replay %*
popd
endlocal
