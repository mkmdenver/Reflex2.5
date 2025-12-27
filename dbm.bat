
@echo off
setlocal
REM =====================================================================
REM  Reflex2 - Legacy Menu App Startup (run from anywhere)
REM  This starts the standalone menu that applies Bedrock schema and
REM  offers Finviz import, Symbol Editor, Polygon backfills, etc.
REM =====================================================================

REM cd to the directory this script resides in (project root if you put it there)
cd /d "c:\projects\reflex2.2" || (
  echo [error] Could not change directory to script location. Exiting.
  exit /b 1
)

REM ---- Python venv ----------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
  echo [setup] Creating virtual environment .venv ...
  python -m venv .venv
)
call ".venv\Scripts\activate.bat"

REM ---- Environment ----------------------------------------------------
REM Set defaults only if not already provided in the shell/user env
if not defined DATABASE_URL (
  REM Adjust if your password/host/db differ. Keep the quotes so "!" is safe.
  set "DATABASE_URL=postgresql://postgres:4Asp@localhost:5432/stock_data"
)
if not defined DBM_INIT_TOKEN set "DBM_INIT_TOKEN=I_UNDERSTAND_DROP_AND_REBUILD"
if not defined DBM_MENU_PORT  set "DBM_MENU_PORT=5001"
set PYTHONUTF8=1

REM Optional: warn if Polygon key not present (used by Backfill page)
if not defined POLYGON_API_KEY (
  echo [warn] POLYGON_API_KEY not set. Backfill page will not work until you set it.
)

REM ---- Dependencies ---------------------------------------------------
echo [setup] Installing/upgrading minimal deps ...
pip install --upgrade --quiet pip
pip install --quiet -r "requirements.txt"

REM ---- Launch ---------------------------------------------------------
echo [run] Starting Legacy Menu App on http://127.0.0.1:%DBM_MENU_PORT%/
start "" "http://127.0.0.1:%DBM_MENU_PORT%/"
python "dbmanager\app.py"
