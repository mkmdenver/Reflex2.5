@echo off
IF EXIST "%~dp0..\.venv\Scripts\activate.bat" ( CALL "%~dp0..\.venv\Scripts\activate.bat" )
set PYTHONPATH=%~dp0..
