@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM Resolve project root (env.bat lives at root)
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

set "ENV_FILE=%ROOT%\.env"
set "LOCAL_FILE=%ROOT%\.env.local"

call :load_env "%ENV_FILE%"
call :load_env "%LOCAL_FILE%"

endlocal & goto :eof

:load_env
set "FILE=%~1"
if not exist "%FILE%" goto :eof

for /f "usebackq delims=" %%L in ("%FILE%") do (
    set "LINE=%%L"
    if not "!LINE!"=="" (
        if not "!LINE:~0,1!"=="#" (
            for /f "tokens=1* delims==" %%A in ("!LINE!") do (
                set "K=%%A"
                set "V=%%B"
                if "!V:~0,1!"=="^"" if "!V:~-1!"=="^"" set "V=!V:~1,-1!"
                set "!K!=!V!"
            )
        )
    )
)
goto :eof
