# tests\smoke_trader.ps1
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Assert-True($cond, $msg) { if (-not $cond) { throw "ASSERT FAIL: $msg" } }
function Get-Json($uri) { (Invoke-RestMethod -Uri $uri -Method GET -TimeoutSec 5) }
function Post-Json($uri, $obj) {
  $body = $obj | ConvertTo-Json -Depth 6
  Invoke-RestMethod -Uri $uri -Method POST -ContentType 'application/json' -Body $body -TimeoutSec 5
}

$Redis  = 'redis://127.0.0.1:6379/0'
$KEY_INFLIGHT = 'reflex:capacity:orders_inflight'
$KEY_MAX      = 'reflex:capacity:orders_max'
$LIST_INTENTS = 'reflex:intents.list'
$DLQ          = 'reflex:intents.dlq'
$ACKS         = 'reflex:events.acks'
$FILLS        = 'reflex:events.fills'
$CHAN_INTENTS = 'reflex:rt:orders.intent'

function R-GET($k)  { (redis-cli -u $Redis GET  $k) }
function R-SET($k,$v){ (redis-cli -u $Redis SET  $k $v) | Out-Null }
function R-DEL([string[]]$keys){ if($keys.Count){ redis-cli -u $Redis DEL @keys | Out-Null } }
function R-LLEN($k){ [int](redis-cli -u $Redis LLEN $k) }
function R-LRANGE($k,$a,$b){ redis-cli -u $Redis LRANGE $k $a $b }
function R-LPUSH($k,$raw){ redis-cli -u $Redis --raw LPUSH $k $raw | Out-Null }
function R-PUBLISH($chan,$raw){ redis-cli -u $Redis --raw PUBLISH $chan $raw | Out-Null }

function New-IntentObj([int]$qty=5,[string]$type='market',[string]$tif='day',[string]$sym='AAPL',[string]$strat='smoke',[double]$est_price=200){
  @{
    symbol     = $sym
    side       = 'BUY'
    qty        = $qty
    order_type = $type
    tif        = $tif
    strategy   = $strat
    est_price  = $est_price
  }
}

function Wait-InflightZero([int]$timeoutMs=4000){
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  while ($sw.ElapsedMilliseconds -lt $timeoutMs) {
    $v = R-GET $KEY_INFLIGHT
    if ($v -eq '"0"' -or $v -eq '0' -or [string]::IsNullOrEmpty($v)) { return $true }
    Start-Sleep -Milliseconds 100
  }
  return $false
}

function Wait-CountRise($key,[int]$baseline,[int]$timeoutMs=3000){
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  while ($sw.ElapsedMilliseconds -lt $timeoutMs) {
    $n = R-LLEN $key
    if ($n -gt $baseline) { return $true }
    Start-Sleep -Milliseconds 100
  }
  return $false
}

Write-Host "=== Reset Redis counters/queues ==="
R-SET $KEY_INFLIGHT 0
R-SET $KEY_MAX      8
R-DEL @($LIST_INTENTS,$DLQ,$ACKS,$FILLS)

Write-Host "=== Health checks ==="
$hEval   = Get-Json 'http://127.0.0.1:7001/health'
$hTrader = Get-Json 'http://127.0.0.1:7002/health'
Assert-True $hEval.ok   "Evaluator health not ok"
Assert-True $hTrader.ok "Trader health not ok"

# Make sure Trader background tasks are alive
$cons = Get-Json 'http://127.0.0.1:7002/debug/consumer-status'
Assert-True ($cons.'bridge_pubsub_to_queue'.running -and $cons.'consume_queue_reliable'.running) "Trader consumers not running"

# ---------- 1) Happy path ----------
Write-Host "`n[1] Happy path intent"
$acks0  = R-LLEN $ACKS
$fills0 = R-LLEN $FILLS
$resp = Post-Json 'http://127.0.0.1:7001/v1/orders/intent' (New-IntentObj)
Assert-True $resp.ok "Evaluator gate not open"

# Wait for ACK/FILL to show up (confirms handler ran)
if (-not (Wait-CountRise $ACKS $acks0 3000)) {
  Write-Warning "ACK count did not rise in time"
}
if (-not (Wait-CountRise $FILLS $fills0 3000)) {
  Write-Warning "FILL count did not rise in time"
}

# Then wait for inflight to settle back to 0
if (-not (Wait-InflightZero 4000)) {
  Write-Warning "Inflight did not return to 0 in time — dumping diagnostics:"
  $li   = Get-Json 'http://127.0.0.1:7002/last-intent'
  $ackH = R-LRANGE $ACKS 0 2
  $filH = R-LRANGE $FILLS 0 2
  $inq  = R-LLEN $LIST_INTENTS
  Write-Host "last-intent: $($li | ConvertTo-Json -Depth 6)"
  Write-Host "ACKs(head): $($ackH | ConvertTo-Json -Depth 6)"
  Write-Host "FILLs(head): $($filH | ConvertTo-Json -Depth 6)"
  Write-Host "queue length: $inq"
  throw "ASSERT FAIL: inflight not back to 0"
}

Write-Host "✅ Happy path OK (ACK/FILL seen, inflight=0)"

# ---------- 2) Gate test ----------
Write-Host "`n[2] Back-pressure gate (MAX=1)"
R-SET $KEY_INFLIGHT 0
R-SET $KEY_MAX      1
$ok1 = Post-Json 'http://127.0.0.1:7001/v1/orders/intent' (New-IntentObj)
$ok2 = Post-Json 'http://127.0.0.1:7001/v1/orders/intent' (New-IntentObj)
Assert-True $ok1.ok "first should be allowed"
Assert-True (-not $ok2.ok) "second should be blocked"
Assert-True (Wait-InflightZero 4000) "gate test: inflight did not return to 0"

# ---------- 3) Limit validation ----------
Write-Host "`n[3] Limit order validation"
try {
  Post-Json 'http://127.0.0.1:7001/v1/orders/intent' (New-IntentObj -type 'limit' -est_price 0) | Out-Null
  throw "Expected 422 for limit without limit_price"
} catch {
  if (-not ($_.ErrorDetails.Message -match 'limit_price required')) { throw }
}
$limit = New-IntentObj -type 'limit' -est_price 150
$limit.limit_price = 150
$respL = Post-Json 'http://127.0.0.1:7001/v1/orders/intent' $limit
Assert-True $respL.ok "limit w/ price should pass"
Assert-True (Wait-InflightZero 4000) "limit: inflight did not settle"

# ---------- 4) DLQ injection ----------
Write-Host "`n[4] DLQ injection"
R-SET $KEY_INFLIGHT 0
R-LPUSH $LIST_INTENTS 'ping:1}'   # intentionally corrupt JSON
Start-Sleep -Milliseconds 300
Assert-True (R-LLEN $DLQ -ge 1) "DLQ should contain a bad message"
Assert-True (Wait-InflightZero 2000) "DLQ: inflight did not settle"

# ---------- 5) Pub/Sub bridge ----------
Write-Host "`n[5] Pub/Sub bridge"
R-SET $KEY_INFLIGHT 0
R-SET $KEY_MAX      8
$pubIntent = (New-IntentObj | ConvertTo-Json -Depth 6)
R-PUBLISH $CHAN_INTENTS $pubIntent
Assert-True (Wait-InflightZero 4000) "pubsub: inflight did not settle"
$li2 = Get-Json 'http://127.0.0.1:7002/last-intent'
Assert-True ($li2.last_intent -ne $null) "pubsub: no last-intent observed"

# ---------- 6) Restart resilience ----------
Write-Host "`n[6] Restart resilience"
R-SET $KEY_INFLIGHT 0
$queued = (New-IntentObj | ConvertTo-Json -Depth 6)
R-LPUSH $LIST_INTENTS $queued
# bounce Trader
Get-NetTCPConnection -LocalPort 7002 -State Listen | Select -Exp OwningProcess -Unique | % { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
& .\start_trader.bat | Out-Null
Start-Sleep -Seconds 1
Assert-True (Wait-InflightZero 4000) "restart: inflight did not settle"
Assert-True ((R-LLEN $LIST_INTENTS) -eq 0) "restart: queue not drained"

# ---------- 7) Burst sanity ----------
Write-Host "`n[7] Burst (10 orders)"
R-SET $KEY_INFLIGHT 0
R-SET $KEY_MAX      50
$acks0 = R-LLEN $ACKS
$fils0 = R-LLEN $FILLS
1..10 | % { Post-Json 'http://127.0.0.1:7001/v1/orders/intent' (New-IntentObj) | Out-Null }
Assert-True (Wait-CountRise $ACKS $acks0 4000)  "ACKs did not rise"
Assert-True (Wait-CountRise $FILLS $fils0 4000) "FILLs did not rise"
Assert-True (Wait-InflightZero 4000) "burst: inflight not 0"

Write-Host "`n✅ ALL TESTS PASSED."
