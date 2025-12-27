@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem ============================================================================
rem add_symbols.bat  (CMD.exe batch, no PowerShell)
rem - Loads ROOT\.env safely (ignores comments/blank lines)
rem - Uses tools\.venv\Scripts\python.exe if present, else system python
rem ============================================================================

set "TOOLS_DIR=%~dp0"
for %%I in ("%TOOLS_DIR%..") do set "ROOT=%%~fI"
set "ENV_FILE=%ROOT%\.env"

echo [PATH] ROOT = %ROOT%
call :load_env "%ENV_FILE%"
if errorlevel 1 exit /b 1

set "PY=%TOOLS_DIR%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo [RUN] %PY% "%TOOLS_DIR%add_symbols.py" %*
%PY% "%TOOLS_DIR%add_symbols.py" %*
exit /b %ERRORLEVEL%

rem ----------------------------------------------------------------------------
rem :load_env  <path-to-.env>
rem - Accepts lines: KEY=VALUE
rem - Skips blank lines and lines starting with #
rem - Uses: set "KEY=VALUE" so &, (, ), spaces, etc. won't explode the batch parser
rem ----------------------------------------------------------------------------
:load_env
set "F=%~1"
if not exist "%F%" (
  echo [ENV][ERR] Missing env file: "%F%"
  exit /b 1
)

echo [ENV] Loading "%F%" ...

for /f "usebackq delims=" %%L in ("%F%") do (
  set "LINE=%%L"
  call :_apply_env_line
)

exit /b 0

:_apply_env_line
setlocal DisableDelayedExpansion
set "S=%LINE%"

rem Trim leading spaces (good enough)
for /f "tokens=* delims= " %%A in ("%S%") do set "S=%%A"

rem Skip blank and comment lines
if "%S%"=="" ( endlocal & exit /b 0 )
if "%S:~0,1%"=="#" ( endlocal & exit /b 0 )

rem Only process if it contains '='
echo(%S%| findstr /c:"=" >nul || ( endlocal & exit /b 0 )

setlocal EnableDelayedExpansion
for /f "tokens=1* delims==" %%K in ("!S!") do (
  endlocal & endlocal & set "%%K=%%L"
)
exit /b 0
