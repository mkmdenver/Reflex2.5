<# 
  Start the full Reflex stack from your project root.
  - Reads config ONLY via your existing start_*.bat which source .env
  - Waits for health on :7000/:7001/:7002
  - Optional: run db_backfill --mode last2 for ALL symbols

  Usage (PowerShell):
    Set-ExecutionPolicy -Scope Process Bypass
    .\scripts\start_all.ps1
#>

param(
  [switch] $BackfillLast2 = $false,   # flip to -BackfillLast2 to run the 2-day backfill
  [int]    $HealthTimeoutSec = 60
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Resolve-Path (Join-Path $here '..') | % Path
Set-Location $root

Write-Host "================== START ALL ==================" -ForegroundColor Cyan
Write-Host "ROOT: $root"
Write-Host "Backfill last2: $BackfillLast2"
Write-Host "================================================`n"

# Ensure venv python exists (for backfill step)
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
  throw "[start_all] venv missing: $py"
}

# Helper: start a .bat in a new window and track PID
$pidsFile = Join-Path $root "scripts\.pids.txt"
Remove-Item $pidsFile -Force -ErrorAction SilentlyContinue | Out-Null
function Start-Unit {
  param([string]$BatFile,[string]$Name)
  $bat = Join-Path $root $BatFile
  if (-not (Test-Path $bat)) { throw "$Name launcher missing: $bat" }
  $proc = Start-Process -FilePath $bat -PassThru
  "$Name=$($proc.Id)" | Out-File -FilePath $pidsFile -Append -Encoding ascii
  return $proc
}

# Launch services (each start_*.bat already loads .env on its own)
Write-Host "[LAUNCH] DataHub" -ForegroundColor Yellow
$ph = Start-Unit -BatFile "start_datahub.bat" -Name "DataHub"

Write-Host "[LAUNCH] Evaluator" -ForegroundColor Yellow
$pe = Start-Unit -BatFile "start_evaluator.bat" -Name "Evaluator"

Write-Host "[LAUNCH] Trader" -ForegroundColor Yellow
$pt = Start-Unit -BatFile "start_trader.bat" -Name "Trader"

# Health check loop
$deadline = (Get-Date).AddSeconds($HealthTimeoutSec)
$ok7000 = $false; $ok7001 = $false; $ok7002 = $false
Write-Host "[CHECK] Waiting for /health on :7000/:7001/:7002 (timeout ${HealthTimeoutSec}s)..." -ForegroundColor Yellow
while ((Get-Date) -lt $deadline) {
  try { $ok7000 = (Invoke-WebRequest http://127.0.0.1:7000/health -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch { }
  try { $ok7001 = (Invoke-WebRequest http://127.0.0.1:7001/health -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch { }
  try { $ok7002 = (Invoke-WebRequest http://127.0.0.1:7002/health -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch { }
  if ($ok7000 -and $ok7001 -and $ok7002) { break }
  Start-Sleep -Milliseconds 500
}
Write-Host ("  :7000 -> {0}" -f ($ok7000 ? "OK" : "WAIT")) 
Write-Host ("  :7001 -> {0}" -f ($ok7001 ? "OK" : "WAIT")) 
Write-Host ("  :7002 -> {0}" -f ($ok7002 ? "OK" : "WAIT")) 

if (-not ($ok7000 -and $ok7001 -and $ok7002)) {
  Write-Warning "Not all services reported healthy before timeout—stack may still be warming up."
}

# Optional: run last two market days backfill (daily + minute; excludes today)
if ($BackfillLast2) {
  Write-Host "`n[RUN] Backfill last2: ALL symbols (daily+minute, no ticks)" -ForegroundColor Yellow
  & $py (Join-Path $root "dbmanager\db_backfill.py") --mode last2 --symbol ALL
}

Write-Host "`nStack running. Quick tips:" -ForegroundColor Cyan
Write-Host "  Send test order (PowerShell):"
Write-Host "    `$body = @{ symbol='AAPL'; side='BUY'; qty=1; order_type='market'; tif='day'; strategy='smoke'; client_tag='auto' } | ConvertTo-Json"
Write-Host "    Invoke-RestMethod http://127.0.0.1:7001/v1/orders/intent -Method POST -ContentType application/json -Body `$body"
Write-Host "  Observe Trader:"
Write-Host "    iwr http://127.0.0.1:7002/last-intent | % { `$_.Content }"
Write-Host "    iwr http://127.0.0.1:7002/debug/last-ack  | % { `$_.Content }"
Write-Host "    iwr http://127.0.0.1:7002/debug/last-fill | % { `$_.Content }"
Write-Host "`nPIDs saved to $pidsFile"
