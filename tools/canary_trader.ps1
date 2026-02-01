#powershell -ExecutionPolicy Bypass -File C:\Projects\Reflex2.5\tools\canary_trader.ps1
#powershell -ExecutionPolicy Bypass -File .\tools\canary_trader.ps1 -TraderBase http://127.0.0.1:7002 -AccountId alpaca:paper


param(
  [string]$TraderBase = "http://127.0.0.1:7002",
  [string]$AccountId  = "alpaca:paper"
)

$ErrorActionPreference = "Stop"

function Hit([string]$label, [string]$method, [string]$url, [string]$bodyJson = "") {
  try {
    if ($method -eq "GET") {
      $r = Invoke-WebRequest $url -TimeoutSec 5
      Write-Host ("PASS  {0}  {1}  {2}" -f $label, $r.StatusCode, $url) -ForegroundColor Green
      return @{ ok=$true; status=$r.StatusCode; body=$r.Content }
    } else {
      $r = Invoke-WebRequest $url -Method Post -ContentType "application/json" -Body $bodyJson -TimeoutSec 5
      Write-Host ("PASS  {0}  {1}  {2}" -f $label, $r.StatusCode, $url) -ForegroundColor Green
      return @{ ok=$true; status=$r.StatusCode; body=$r.Content }
    }
  } catch {
    $resp = $_.Exception.Response
    if ($resp) {
      $status = [int]$resp.StatusCode
      $txt = $resp.Content.ReadAsStringAsync().Result
      Write-Host ("FAIL  {0}  {1}  {2}" -f $label, $status, $url) -ForegroundColor Red
      if ($txt) { Write-Host ("      body: {0}" -f $txt) -ForegroundColor DarkRed }
      return @{ ok=$false; status=$status; body=$txt }
    } else {
      Write-Host ("FAIL  {0}  (no response)  {1}" -f $label, $url) -ForegroundColor Red
      Write-Host ("      err:  {0}" -f $_.Exception.Message) -ForegroundColor DarkRed
      return @{ ok=$false; status=-1; body=$_.Exception.Message }
    }
  }
}

Write-Host "=== Trader Canary ===" -ForegroundColor Cyan
Write-Host ("TraderBase: {0}" -f $TraderBase)
Write-Host ("AccountId : {0}" -f $AccountId)
Write-Host ""

$results = @()

$results += Hit "health"   "GET"  "$TraderBase/v1/health"
$results += Hit "time"     "GET"  "$TraderBase/v1/time"
$results += Hit "accounts" "GET"  "$TraderBase/v1/accounts"
$results += Hit "overview" "GET"  "$TraderBase/v1/portfolio/overview"
$results += Hit "pos"      "GET"  "$TraderBase/v1/portfolio/positions?account_id=$([uri]::EscapeDataString($AccountId))"
$results += Hit "ordersA"  "GET"  "$TraderBase/v1/orders?account_id=$([uri]::EscapeDataString($AccountId))&status=active"
$results += Hit "ordersC"  "GET"  "$TraderBase/v1/orders?account_id=$([uri]::EscapeDataString($AccountId))&status=closed"
$results += Hit "events"   "GET"  "$TraderBase/v1/events"

# Route existence check: should be 422 (validation error), NOT 404
$results += Hit "placeRoute" "POST" "$TraderBase/v1/orders/place" "{}"

Write-Host ""
$fail = $results | Where-Object { -not $_.ok }
if ($fail.Count -gt 0) {
  Write-Host ("=== FAIL ({0} failed) ===" -f $fail.Count) -ForegroundColor Red
  exit 1
} else {
  Write-Host "=== OK (all checks passed) ===" -ForegroundColor Green
  exit 0
}
