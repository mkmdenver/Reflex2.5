@echo off
setlocal

REM Reflex2 - Replay Intents Publisher (with clock pulses)
REM KISS: relies on env.bat to set PY and load .env/.env.local

call "%~dp0env.bat" || exit /b 1

set REFLEX_MODE=REPLAY

if "%REFLEX_INSTANCE_ID%"=="" set REFLEX_INSTANCE_ID=live
if "%REPLAY_CLOCK_CHANNEL%"=="" set REPLAY_CLOCK_CHANNEL=replay.clock.%REFLEX_INSTANCE_ID%

echo [RUN] replay_intents  instance=%REFLEX_INSTANCE_ID%  clock=%REPLAY_CLOCK_CHANNEL%
"%PY%" tools\intent_replayer.py %*
exit /b %ERRORLEVEL%
