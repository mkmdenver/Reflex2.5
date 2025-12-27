@echo off
setlocal
cd /d %~dp0

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m evaluator.filters.filter2_cameron_from_ross %*
) else (
    python -m evaluator.filters.filter2_cameron_from_ross %*
)

endlocal
