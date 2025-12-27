@echo off
rem load_env.bat  --  call this with:  call scripts\load_env.bat .env.liveA
if "%~1"=="" (
  echo Usage: call scripts\load_env.bat path\to\.env
  exit /b 1
)
for /f "usebackq tokens=1,* delims== eol=#" %%A in ("%~1") do (
  set "%%A=%%B"
)
exit /b 0
