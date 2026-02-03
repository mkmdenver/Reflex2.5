@echo off
setlocal

set "ROOT=%~dp0.."
cd /d "%ROOT%"

call "%ROOT%\env.bat"
if errorlevel 1 (
  echo [tap_intents] ERROR: env.bat failed
  exit /b 1
)

if not exist "%ROOT%\.venv\Scripts\python.exe" (
  echo [tap_intents] Missing venv python: "%ROOT%\.venv\Scripts\python.exe"
  exit /b 1
)

set "PTI_FEED_MODE=LIVE" 
set "PTI_INTENT_CHANNEL=eval.intent.live"

echo [RUNNING] "%ROOT%\tools\tap_intents.py"
"%ROOT%\.venv\Scripts\python.exe" "%ROOT%\tools\TAP_intents.py" %*

endlocal
