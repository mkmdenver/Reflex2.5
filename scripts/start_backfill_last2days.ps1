# scripts/start_backfill_last2days.ps1
# Backfill minute + daily for the last 2 trading days (excludes today). No holidays.

param(
  [int]$MaxParallel = 6,          # tune if your provider squawks
  [double]$SleepBetween = 0.05     # seconds between requests
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-LastNTradingDays([int]$n) {
  $days = @()
  $d = (Get-Date).Date.AddDays(-1)   # start from yesterday
  while ($days.Count -lt $n) {
    if ($d.DayOfWeek -ne 'Saturday' -and $d.DayOfWeek -ne 'Sunday') {
      $days += $d
    }
    $d = $d.AddDays(-1)
  }
  # return newest-first
  $days | Sort-Object
}

# --- compute window (last two trading days, newest-first, contiguous) ---
$two = Get-LastNTradingDays -n 2
$start = $two[0].ToString('yyyy-MM-dd')
$end   = $two[-1].ToString('yyyy-MM-dd')

Write-Host "[plan] Backfill window: $start → $end (2 trading days), min + daily" -ForegroundColor Cyan

# --- base env (project expects these) ---
$root = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
Push-Location $root
try {
  # Required ENV (adjust if yours differ)
  # Note: using Set-Item Env:... avoids the ${env:NAME} gotcha you hit earlier.
  Set-Item Env:REFLEX__START_DATE        $start
  Set-Item Env:REFLEX__END_DATE          $end
  Set-Item Env:REFLEX__ORDER             'days-first'
  Set-Item Env:REFLEX__SLEEP             ([string]$SleepBetween)
  Set-Item Env:REFLEX__MAX_PARALLEL      ([string]$MaxParallel)
  Set-Item Env:REFLEX__HUB_MODE          'BACKFILL'
  # If you use symbols_sql to target a subset, set it here. Otherwise DataHub default query applies.
  # Set-Item Env:REFLEX__SYMBOLS_SQL "FROM symbol_metadata ORDER BY symbol"

  Write-Host "[env] START=$env:REFLEX__START_DATE  END=$env:REFLEX__END_DATE"
  Write-Host "[env] order=$env:REFLEX__ORDER  sleep=$env:REFLEX__SLEEP  max_parallel=$env:REFLEX__MAX_PARALLEL"

  # --- kick minute bars ---
  if (Test-Path ".\scripts\launch_minutes.ps1") {
    Write-Host "[run] launch_minutes.ps1" -ForegroundColor Green
    .\scripts\launch_minutes.ps1
  } else {
    Write-Host "[warn] scripts\launch_minutes.ps1 not found. Skipping minutes." -ForegroundColor Yellow
  }

  # --- kick daily bars ---
  if (Test-Path ".\scripts\launch_daily.ps1") {
    Write-Host "[run] launch_daily.ps1" -ForegroundColor Green
    .\scripts\launch_daily.ps1
  } else {
    Write-Host "[warn] scripts\launch_daily.ps1 not found. Skipping daily." -ForegroundColor Yellow
  }
}
finally {
  Pop-Location
}
