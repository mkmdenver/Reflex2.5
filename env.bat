@echo off
REM env.bat — load .env and .env.local into the CURRENT process environment.
REM IMPORTANT: Do NOT use setlocal/endlocal at top-level, or env vars vanish on return.

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

REM If caller passes a specific file path, load only that file.
if not "%~1"=="" (
  call :load_file "%~1"
  goto :eof
)

REM Default: load root .env then .env.local
call :load_file "%ROOT%\.env"
if exist "%ROOT%\.env.local" call :load_file "%ROOT%\.env.local"

goto :eof


:load_file
set "FILE=%~1"
if not exist "%FILE%" goto :eof

for /f "usebackq delims=" %%L in ("%FILE%") do (
  set "LINE=%%L"
  if not "%LINE%"=="" (
    if not "%LINE:~0,1%"=="#" (
      for /f "tokens=1* delims==" %%A in ("%%L") do (
        if not "%%A"=="" set "%%A=%%B"
      )
    )
  )
)

goto :eof
