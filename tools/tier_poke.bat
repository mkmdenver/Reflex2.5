@echo off
setlocal

REM Always run from repo root
set "ROOT=%~dp0.."
cd /d "%ROOT%"

REM Load environment (root env.bat loads .env + .env.local in your system)
call "%ROOT%\env.bat"
if errorlevel 1 (
  echo [tier_poke] ERROR: env.bat failed
  exit /b 1
)

REM Require venv python (consistent with your other tools)
if not exist "%ROOT%\.venv\Scripts\python.exe" (
  echo [tier_poke] Missing venv python: "%ROOT%\.venv\Scripts\python.exe"
  exit /b 1
)

"%ROOT%\.venv\Scripts\python.exe" "%ROOT%\tools\tier_poke.py" %*

endlocal
