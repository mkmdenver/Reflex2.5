@echo off
setlocal EnableDelayedExpansion
set "LOG=%~1"
if "%LOG%"=="" exit /b 1

:loop
set "line="
set /p line=
if errorlevel 1 exit /b 0
echo !line!
>> "%LOG%" echo !line!
goto loop
