
setlocal EnableExtensions EnableDelayedExpansion
set "ROOT=%~dp0"
pushd "%ROOT%"

call "%ROOT%\setup_env.bat" || (echo [ERROR] venv setup failed & exit /b 1)

rem --- Trader FIRST (capacity + consumer group)
start "TRADER" /MIN "%ROOT%\start_trader.bat"
timeout /t 2 >nul

rem --- DataHub SECOND (quote/trade fan-out via Pub/Sub)
start "DATAHUB" /MIN "%ROOT%\start_datahub.bat"
timeout /t 2 >nul

rem --- Evaluator LAST (will gate on orders_inflight < orders_max)
start "EVALUATOR" /MIN "%ROOT%\start_evaluator.bat"

echo [OK] Launched: Trader, DataHub, Evaluator.
popd
exit /b 0
