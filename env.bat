@echo off
REM env.bat — load .env and .env.local into the CURRENT process environment.
REM IMPORTANT: Do NOT use setlocal/endlocal in this file, or variables vanish on return.
REM CONTRACT:
REM   - .env/.env.local contain BASE names (no instance scoping baked in).
REM   - Startup BATs decide LIVE/REPLAY + REFLEX_INSTANCE_ID and may set scoped channels.
REM   - This loader loads key/value pairs and expands {instance_id} tokens AFTER loading
REM     once REFLEX_INSTANCE_ID is known (even if REFLEX_INSTANCE_ID is defined inside .env).

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

REM If caller passes a specific file path, load only that file.
if not "%~1"=="" (
  call :load_file "%~1"
  call :expand_instance_tokens
  goto :eof
)

REM Default: load root .env then .env.local (if present)
call :load_file "%ROOT%\.env"
if exist "%ROOT%\.env.local" call :load_file "%ROOT%\.env.local"

call :expand_instance_tokens
goto :eof

:load_file
set "FILE=%~1"
if not exist "%FILE%" goto :eof

REM Notes:
REM - eol=# skips comment lines starting with #
REM - "tokens=1* delims==" keeps VALUE intact even if it contains '='
REM - blank lines are skipped by FOR /F
for /f "usebackq eol=# delims=" %%L in ("%FILE%") do (
  for /f "tokens=1* delims==" %%A in ("%%L") do (
    if not "%%A"=="" (
      set "%%A=%%B"
    )
  )
)
goto :eof

:expand_instance_tokens
REM Expand {instance_id} in any loaded environment variable values.
REM Works even if REFLEX_INSTANCE_ID was defined inside .env (because this runs AFTER load).
if "%REFLEX_INSTANCE_ID%"=="" goto :eof

REM Find all vars whose current value contains "{instance_id}" and replace.
for /f "delims=" %%V in ('set ^| findstr /c:"{instance_id}"') do (
  for /f "tokens=1* delims==" %%A in ("%%V") do (
    set "RAW=%%B"
    call set "RAW=%%RAW:{instance_id}=%REFLEX_INSTANCE_ID%%%"
    set "%%A=%RAW%"
  )
)
goto :eof
