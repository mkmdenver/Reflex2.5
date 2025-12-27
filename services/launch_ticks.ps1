# scripts\launch_ticks.ps1
# Non-interactive launcher for ticks_backfill.
# - Params are optional; fall back to env vars AFTER the param() block.
# - No Set-StrictMode until after defaults are assigned (avoids uninitialized var errors).

param(
  [string]$StartDate = $env:REFLEX__START_DATE,
  [string]$EndDate   = $env:REFLEX__END_DATE,
  [string]$Table,
  [string]$SymbolsSql,
  [Nullable[int]]$Limit,
  [Nullable[double]]$Sleep,
  [switch]$IncludeOTC
)

# Resolve repo root
$ScriptDir = Split-Path -Parent $PSCommandPath
$RepoRoot  = Split-Path -Parent $ScriptDir
Set-Location $RepoRoot

# ---- Assign defaults safely (no ternary, no assignment expressions in param) ----
if (-not $Table)      { $Table = if ($env:REFLEX__TABLE) { $env:REFLEX__TABLE } else { 'tick_data' } }
if (-not $SymbolsSql) { $SymbolsSql = if ($env:REFLEX__SYMBOLS_SQL) { $env:REFLEX__SYMBOLS_SQL } else { 'FROM symbol_metadata ORDER BY symbol' } }
if (-not $Limit.HasValue) { $Limit = if ($env:REFLEX__LIMIT) { [int]$env:REFLEX__LIMIT } else { 50000 } }
if (-not $Sleep.HasValue) { $Sleep = if ($env:REFLEX__SLEEP_SECONDS) { [double]$env:REFLEX__SLEEP_SECONDS } else { 0.05 } }

# Now it’s safe to enable stricter behavior if you want it
# Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---- Required dates validation ----
if ([string]::IsNullOrWhiteSpace($StartDate) -or [string]::IsNullOrWhiteSpace($EndDate)) {
  throw "StartDate/EndDate are required. Set REFLEX__START_DATE / REFLEX__END_DATE env vars or pass -StartDate / -EndDate."
}

# ---- Export for Python process ----
$env:REFLEX__START_DATE    = $StartDate
$env:REFLEX__END_DATE      = $EndDate
$env:REFLEX__TABLE         = $Table
$env:REFLEX__SYMBOLS_SQL   = $SymbolsSql
$env:REFLEX__LIMIT         = "$Limit"
$env:REFLEX__SLEEP_SECONDS = "$Sleep"
$env:REFLEX__INCLUDE_OTC   = $(if ($IncludeOTC) { "true" } else { "false" })

# ---- Prefer venv Python if present ----
$py = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

# ---- Visibility banner ----
Write-Host "[root] $RepoRoot"
Write-Host "[dsn]  $($env:PG_DSN)"
Write-Host "[pg]   PGHOST=$($env:PGHOST) PGPORT=$($env:PGPORT) PGUSER=$($env:PGUSER) PGDATABASE=$($env:PGDATABASE)"
Write-Host "[plan] $StartDate -> $EndDate  table=$Table  symbols=[$SymbolsSql]  limit=$Limit  sleep=$Sleep  include_otc=$($env:REFLEX__INCLUDE_OTC)"

# ---- Launch ----
& $py -m dbmanager.ticks_backfill
exit $LASTEXITCODE

