@echo off
setlocal EnableExtensions EnableDelayedExpansion

set ROOT=%~dp0
cd /d "%ROOT%"

call env.bat

echo [RUN] Ross filter tap on eval.ross_filter_stream ...
python -m evaluator.debug_ross_filter_tap

endlocal
