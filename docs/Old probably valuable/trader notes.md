Set the knob:

$env:REFLEX__RECON_HEARTBEAT_SEC="60"

3) Simple “halt / arm” controls you can hit fast

Panic halt (stop any execution path that checks the flag):

redis-cli -u redis://127.0.0.1:6379 SET reflex:trading:halt 1
redis-cli -u redis://127.0.0.1:6379 SET reflex:trading:halt_reason "manual_halt"


Lift halt:

redis-cli -u redis://127.0.0.1:6379 DEL reflex:trading:halt reflex:trading:halt_reason

Quick validation loop (right now)

Re-run reconcile to confirm account data appears:

.\.venv\Scripts\python.exe trader\reconcile_startup.py


Start Trader with heartbeat on:

$env:REFLEX__RECONCILE_ON_START="1"
$env:REFLEX__HALT_ON_RECONCILE_DIFF="1"
$env:REFLEX__RECON_HEARTBEAT_SEC="60"
.\.venv\Scripts\python.exe -m trader.broker_worker


If the cockpit shows SPY x2 and the account block (cash/buying power/PDT), you’re fully closed-loop.


Runbook (concise)

From C:\Projects\Reflex2.2:

# 0) (optional) clean queues on boot
$env:REFLEX__STARTUP_QUEUE_ACTION="drain"     # or stats|purge|none
# $env:REFLEX__STARTUP_QUEUE_KEYS="reflex:intents.auto,reflex:events.orders"  # optional override

# 1) reconcile policy
$env:REFLEX__RECONCILE_ON_START="1"
$env:REFLEX__HALT_ON_RECONCILE_DIFF="1"
$env:REFLEX__RECON_HEARTBEAT_SEC="60"

# 2) broker creds (paper)
$env:ALPACA_BASE="https://paper-api.alpaca.markets"
$env:ALPACA_API_KEY_ID="YOUR_PAPER_KEY"
$env:ALPACA_API_SECRET_KEY="YOUR_PAPER_SECRET"

# 3) start worker
.\.venv\Scripts\python.exe -m trader.broker_worker


Halt / unhalt fast:

# Halt
redis-cli -u redis://127.0.0.1:6379 SET reflex:trading:halt 1
redis-cli -u redis://127.0.0.1:6379 SET reflex:trading:halt_reason "manual_halt"

# Lift halt
redis-cli -u redis://127.0.0.1:6379 DEL reflex:trading:halt reflex:trading:hal