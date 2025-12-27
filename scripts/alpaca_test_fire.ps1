# scripts/alpaca_smoke_and_fire.ps1
# Robust Alpaca auth check + optional test order (after-hours friendly)

# --- Helper: mask secrets but show length ---


#cd C:\Projects\Reflex2.2

# Set env in THIS shell (paper example)
#$env:ALPACA_BASE="https://paper-api.alpaca.markets"
#$env:ALPACA_API_KEY_ID="PKODKSJM7MEWDMPT7YCHGJFWRH"
#$env:ALPACA_API_SECRET_KEY="Ge67quAkjF6jKz2obLH4S6we6NVJzH98MxKF7FBzhQTV"

# Run
#powershell -ExecutionPolicy Bypass -File .\scripts\alpaca_smoke_and_fire.ps1




function Mask($s) {
  if (-not $s) { return "<EMPTY>" }
  $trimmed = $s.Trim()
  $n = $trimmed.Length
  if ($n -le 6) { return ('*' * $n) }
  return ($trimmed.Substring(0,3) + ('*' * ($n-6)) + $trimmed.Substring($n-3))
}

# --- 0) Read env + basic sanity ---
$base   = $env:ALPACA_BASE
$key    = $env:ALPACA_API_KEY_ID
$secret = $env:ALPACA_API_SECRET_KEY

Write-Host "== ENV CHECK ==" -ForegroundColor Cyan
Write-Host ("ALPACA_BASE         : {0}" -f ($base ?? "<EMPTY>"))
Write-Host ("ALPACA_API_KEY_ID   : {0}" -f (Mask $key))
Write-Host ("ALPACA_API_SECRET_KEY: {0}" -f (Mask $secret))

# Common gotchas
if (-not $base) {
  Write-Host "ERROR: ALPACA_BASE is empty. For paper, use https://paper-api.alpaca.markets" -ForegroundColor Red
  return
}
if (-not $key -or -not $secret) {
  Write-Host "ERROR: Key/secret missing. Set in THIS shell before running:" -ForegroundColor Red
  Write-Host '$env:ALPACA_BASE="https://paper-api.alpaca.markets"'
  Write-Host '$env:ALPACA_API_KEY_ID="..."; $env:ALPACA_API_SECRET_KEY="..."'
  return
}

# Warn if base doesn't look like paper/live
if ($base -notmatch "alpaca\.markets") {
  Write-Host "WARN: ALPACA_BASE looks odd: $base" -ForegroundColor Yellow
}

# Trim (in case of trailing spaces/newlines copied)
$base   = $base.Trim()
$key    = $key.Trim()
$secret = $secret.Trim()

# --- 1) Account probe (auth sanity) ---
Write-Host "`n== ACCOUNT PROBE ==" -ForegroundColor Cyan
$acctUrl = "$base/v2/account"

try {
  $acct = Invoke-RestMethod -Uri $acctUrl `
    -Headers @{ "APCA-API-KEY-ID"=$key; "APCA-API-SECRET-KEY"=$secret } `
    -Method GET -TimeoutSec 20
  Write-Host "OK: Authenticated. Account ID: $($acct.id)  Status: $($acct.status)  PDT: $($acct.pattern_day_trader)" -ForegroundColor Green
  Write-Host "Cash: $($acct.cash)  BuyingPower: $($acct.buying_power)  Portfolio: $($acct.portfolio_value)"
}
catch {
  Write-Host "FAIL: Account probe failed" -ForegroundColor Red
  if ($_.Exception.Response -and $_.Exception.Response.ContentLength -ge 0) {
    $sr = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
    $body = $sr.ReadToEnd()
    Write-Host ("HTTP {0} {1}" -f [int]$_.Exception.Response.StatusCode, $_.Exception.Response.StatusDescription)
    Write-Host "Body: $body"
  } else {
    Write-Host $_.Exception.Message
  }
  Write-Host "`nChecklist:" -ForegroundColor Yellow
  Write-Host "  • Keys belong to PAPER if using paper base (https://paper-api.alpaca.markets)."
  Write-Host "  • No trailing spaces in env vars (we Trim() but double-check)."
  Write-Host "  • Header names EXACT: APCA-API-KEY-ID / APCA-API-SECRET-KEY."
  Write-Host "  • This shell actually has the env set (echo them)."
  return
}

# --- 2) Optional FIRE: tiny after-hours market order ---
Write-Host "`n== TEST ORDER (SPY 1 @ market, extended_hours) ==" -ForegroundColor Cyan
$ordBody = @{
  symbol         = "SPY"
  qty            = "1"
  side           = "buy"
  type           = "market"
  time_in_force  = "day"
  extended_hours = $true
} | ConvertTo-Json

try {
  $order = Invoke-RestMethod -Uri "$base/v2/orders" `
    -Headers @{ "APCA-API-KEY-ID"=$key; "APCA-API-SECRET-KEY"=$secret } `
    -ContentType 'application/json' -Method POST -Body $ordBody -TimeoutSec 20
  Write-Host ("Submitted: id={0} symbol={1} status={2} submitted_at={3}" -f $order.id, $order.symbol, $order.status, $order.submitted_at) -ForegroundColor Green
}
catch {
  Write-Host "FAIL: Order submit failed" -ForegroundColor Red
  if ($_.Exception.Response) {
    $sr = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
    $body = $sr.ReadToEnd()
    Write-Host ("HTTP {0} {1}" -f [int]$_.Exception.Response.StatusCode, $_.Exception.Response.StatusDescription)
    Write-Host "Body: $body"
  } else {
    Write-Host $_.Exception.Message
  }
  return
}

Start-Sleep -Seconds 5

# --- 3) Open orders + positions snapshot ---
Write-Host "`n== OPEN ORDERS ==" -ForegroundColor Cyan
try {
  $open = Invoke-RestMethod -Uri "$base/v2/orders?status=open&limit=50" `
    -Headers @{ "APCA-API-KEY-ID"=$key; "APCA-API-SECRET-KEY"=$secret } `
    -Method GET -TimeoutSec 20
  $open | Select-Object id,symbol,qty,side,type,status,submitted_at | Format-Table
}
catch {
  Write-Host "WARN: open orders fetch failed" -ForegroundColor Yellow
}

Write-Host "`n== POSITIONS ==" -ForegroundColor Cyan
try {
  $pos = Invoke-RestMethod -Uri "$base/v2/positions" `
    -Headers @{ "APCA-API-KEY-ID"=$key; "APCA-API-SECRET-KEY"=$secret } `
    -Method GET -TimeoutSec 20
  $pos | Select-Object symbol,qty,avg_entry_price,market_value,unrealized_pl | Format-Table
}
catch {
  Write-Host "WARN: positions fetch failed" -ForegroundColor Yellow
}

Write-Host "`nDone." -ForegroundColor Green
