@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM --- load .env so we can read GARNET_URL / instance (optional) ---
set "ROOT=%~dp0..\"
if exist "%ROOT%\.env" (
  for /f "usebackq tokens=1,* delims== eol=#" %%A in ("%ROOT%\.env") do set "%%A=%%B"
)
if exist "%ROOT%\.env.local" (
  for /f "usebackq tokens=1,* delims== eol=#" %%A in ("%ROOT%\.env.local") do set "%%A=%%B"
)

if not defined GARNET_URL set "GARNET_URL=redis://127.0.0.1:6379"

where redis-cli >NUL 2>&1
if not %ERRORLEVEL%==0 (
  echo [WARN] redis-cli not found; cannot nuke queues
  exit /b 0
)

echo [NUKE] Deleting Redis keys matching reflex:*
set "CUR=0"

:scanloop
REM --raw: first line is NEXT cursor, subsequent lines are keys
for /f "usebackq delims=" %%L in (`redis-cli --raw -u "%GARNET_URL%" SCAN %CUR% MATCH reflex:* COUNT 1000`) do (
  if not defined NEXT (
    set "NEXT=%%L"
  ) else (
    if not "%%L"=="" (
      redis-cli -u "%GARNET_URL%" DEL "%%L" >NUL
    )
  )
)

if not defined NEXT set "NEXT=0"
set "CUR=%NEXT%"
set "NEXT="

if not "%CUR%"=="0" goto scanloop

echo [NUKE] Done.
exit /b 0
