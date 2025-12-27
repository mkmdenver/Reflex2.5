# Simple one-shot order sender. No env changes.
param(
  [string]$Symbol = "AAPL",
  [ValidateSet("BUY","SELL")] [string]$Side = "BUY",
  [int]$Qty = 1,
  [ValidateSet("market","limit")] [string]$OrderType = "market",
  [string]$Tif = "day",
  [string]$Strategy = "smoke",
  [string]$ClientTag = "auto",
  [double]$LimitPrice = 0
)

$uri = "http://127.0.0.1:7001/v1/orders/intent"

$payload = [ordered]@{
  symbol     = $Symbol
  side       = $Side
  qty        = $Qty
  order_type = $OrderType
  tif        = $Tif
  strategy   = $Strategy
  client_tag = $ClientTag
}

if ($OrderType -eq "limit" -and $LimitPrice -gt 0) {
  $payload.limit_price = [double]$LimitPrice
}

$body = $payload | ConvertTo-Json -Depth 5

try {
  $res = Invoke-RestMethod -Uri $uri -Method POST -ContentType "application/json" -Body $body
  $res | Format-Table -AutoSize
} catch {
  Write-Host $_.Exception.Message -ForegroundColor Red
  if ($_.ErrorDetails.Message) { Write-Host $_.ErrorDetails.Message -ForegroundColor Red }
}
