<#  scripts\alpca_test_fire.ps1  (PS7-safe)
    Smoke test + after-hours order. Defaults to LIMIT (market is rejected after-hours).
#>
[CmdletBinding()]
param(
  [string]$Symbol = "SPY",
  [ValidateSet("buy","sell")] [string]$Side = "buy",
  [int]$Qty = 1,
  [ValidateSet("auto","market","limit")] [string]$Type = "auto",
  [nullable[decimal]]$LimitPrice = $null,
  [bool]$ExtendedHours = $true
)

function Mask($s){ if(-not $s){return "<EMPTY>"} $t=$s.Trim(); if($t.Length-le 6){return ('*'*$t.Length)} $t.Substring(0,3)+(''*($t.Length-6))+$t.Substring($t.Length-3) }
function Get-RespBody([object]$err){
  try{
    $resp = $err.Exception.Response
    if($resp -is [System.Net.Http.HttpResponseMessage]){
      $status=[int]$resp.StatusCode; $reason=$resp.ReasonPhrase
      $body=$resp.Content.ReadAsStringAsync().GetAwaiter().GetResult()
      return @{status=$status;reason=$reason;body=$body}
    }
  }catch{}
  if($err.ErrorDetails -and $err.ErrorDetails.Message){ return @{status=$null;reason=$null;body=$err.ErrorDetails.Message} }
  return @{status=$null;reason=$null;body=$err.Exception.Message}
}

# ENV
$base=$env:ALPACA_BASE; $key=$env:ALPACA_API_KEY_ID; $secret=$env:ALPACA_API_SECRET_KEY
Write-Host "== ENV CHECK ==" -ForegroundColor Cyan
Write-Host ("ALPACA_BASE          : {0}" -f ($base ?? "<EMPTY>"))
Write-Host ("ALPACA_API_KEY_ID    : {0}" -f (Mask $key))
Write-Host ("ALPACA_API_SECRET_KEY: {0}" -f (Mask $secret))
if(-not $base){Write-Host "ERROR: ALPACA_BASE not set." -ForegroundColor Red; return}
if(-not $key){Write-Host "ERROR: ALPACA_API_KEY_ID not set." -ForegroundColor Red; return}
if(-not $secret){Write-Host "ERROR: ALPACA_API_SECRET_KEY not set." -ForegroundColor Red; return}
$base=$base.Trim(); $key=$key.Trim(); $secret=$secret.Trim()

# ACCOUNT PROBE
Write-Host "`n== ACCOUNT PROBE ==" -ForegroundColor Cyan
try{
  $acct = Invoke-RestMethod -Uri "$base/v2/account" -Headers @{ "APCA-API-KEY-ID"=$key; "APCA-API-SECRET-KEY"=$secret } -Method GET -TimeoutSec 20
  Write-Host ("OK: Authenticated. Account ID={0}  Status={1}  PDT={2}" -f $acct.id,$acct.status,$acct.pattern_day_trader) -ForegroundColor Green
  Write-Host ("Cash={0}  BuyingPower={1}  Portfolio={2}" -f $acct.cash,$acct.buying_power,$acct.portfolio_value)
}catch{
  $info=Get-RespBody $_; Write-Host "FAIL: Account probe failed" -ForegroundColor Red
  if($info.status){Write-Host ("HTTP {0} {1}" -f $info.status,$info.reason)}; Write-Host "Body: $($info.body)"; return
}

# ORDER PLAN (default to LIMIT after-hours)
$resolvedType = if($Type -eq "auto"){ "limit" } else { $Type }
if($resolvedType -eq "limit" -and -not $LimitPrice){
  $LimitPrice = if($Side -eq "buy"){ 1000 } else { 1 }
}
Write-Host "`n== ORDER PLAN ==" -ForegroundColor Cyan
Write-Host ("symbol={0} side={1} qty={2} type={3} limit={4} extended_hours={5}" -f $Symbol,$Side,$Qty,$resolvedType,($LimitPrice ?? "<n/a>"),$ExtendedHours)

$payload = @{
  symbol=$Symbol; qty="$Qty"; side=$Side; type=$resolvedType; time_in_force="day"; extended_hours=$ExtendedHours
}
if($resolvedType -eq "limit"){ $payload.limit_price = "$LimitPrice" }
$payloadJson = $payload | ConvertTo-Json -Depth 4

# SUBMIT
Write-Host "`n== SUBMIT ORDER ==" -ForegroundColor Cyan
try{
  $order = Invoke-RestMethod -Uri "$base/v2/orders" -Headers @{ "APCA-API-KEY-ID"=$key; "APCA-API-SECRET-KEY"=$secret } -ContentType 'application/json' -Method POST -Body $payloadJson -TimeoutSec 20
  Write-Host ("Submitted: id={0} symbol={1} side={2} type={3} status={4} submitted_at={5}" -f $order.id,$order.symbol,$order.side,$order.type,$order.status,$order.submitted_at) -ForegroundColor Green
}catch{
  $info=Get-RespBody $_; Write-Host "FAIL: Order submit failed" -ForegroundColor Red
  if($info.status){Write-Host ("HTTP {0} {1}" -f $info.status,$info.reason)}; Write-Host "Body: $($info.body)"; return
}

Start-Sleep -Seconds 5

# SNAPSHOTS
Write-Host "`n== OPEN ORDERS ==" -ForegroundColor Cyan
try{
  $open = Invoke-RestMethod -Uri "$base/v2/orders?status=open&limit=50" -Headers @{ "APCA-API-KEY-ID"=$key; "APCA-API-SECRET-KEY"=$secret } -Method GET -TimeoutSec 20
  if($open){ $open | Select-Object id,symbol,qty,side,type,status,submitted_at | Format-Table } else { Write-Host "(none)" }
}catch{
  $info=Get-RespBody $_; Write-Host "WARN: open orders fetch failed" -ForegroundColor Yellow
  if($info.status){Write-Host ("HTTP {0} {1}" -f $info.status,$info.reason)}; Write-Host "Body: $($info.body)"
}

Write-Host "`n== POSITIONS ==" -ForegroundColor Cyan
try{
  $pos = Invoke-RestMethod -Uri "$base/v2/positions" -Headers @{ "APCA-API-KEY-ID"=$key; "APCA-API-SECRET-KEY"=$secret } -Method GET -TimeoutSec 20
  if($pos){ $pos | Select-Object symbol,qty,avg_entry_price,market_value,unrealized_pl | Format-Table } else { Write-Host "(none)" }
}catch{
  $info=Get-RespBody $_; Write-Host "WARN: positions fetch failed" -ForegroundColor Yellow
  if($info.status){Write-Host ("HTTP {0} {1}" -f $info.status,$info.reason)}; Write-Host "Body: $($info.body)"
}

Write-Host "`nDone." -ForegroundColor Green
