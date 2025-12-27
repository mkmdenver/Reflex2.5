# clean slate in DB0
redis-cli -u redis://127.0.0.1:6379/0 MSET `
  reflex:cap:auto_inflight 0 reflex:cap:auto_max 8 `
  reflex:cap:manual_inflight 0 reflex:cap:manual_max 2 `
  reflex:cap:dev_inflight 0 reflex:cap:dev_max 8


  
# from C:\Projects\Reflex2.2
$env:TRADER_QUEUE = "reflex:intents:auto"
$env:BROKER = "sim"   # flip to "alpaca" after keys are set
.\start_trader.bat
.\start_evaluator.bat
.\start_datahub.bat

$body = @{ symbol="AAPL"; side="BUY"; qty=5; order_type="market"; tif="day"; strategy="smoke"; client_tag="auto" } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:7001/v1/orders/intent -Method POST -ContentType application/json -Body $body
# expect: ok=True gate=open; last-intent shows up; ACK/FILL events append
