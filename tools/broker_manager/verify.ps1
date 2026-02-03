# ===== Reflex verifier v2 (loads .env first) =====
$ErrorActionPreference = 'Stop'
$root = "C:\Projects\Reflex2.2"
Set-Location $root

function Import-DotEnv {
  param([string]$Path = ".\.env")
  if (!(Test-Path -LiteralPath $Path)) { throw ".env not found at $Path" }
  Get-Content -LiteralPath $Path | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { return }
    $eq = $line.IndexOf("="); if ($eq -lt 1) { return }
    $k = $line.Substring(0, $eq).Trim()
    $v = $line.Substring($eq + 1)
    if ($v -match '^\s*"(.*)"\s*$') { $v = $matches[1] }
    elseif ($v -match "^\s*'(.*)'\s*$") { $v = $matches[1] }
    Set-Item -Path "Env:$k" -Value $v
  }
}

function psqlc { param($dsn,$sql)
  if (-not $dsn) { throw "psqlc: DSN missing" }
  psql $dsn -v ON_ERROR_STOP=1 -X -q -c $sql
}

# 1) Load .env into THIS admin PowerShell session
Import-DotEnv

# 2) Resolve env
$env:PYTHONPATH = $root
$pgMain   = $env:REFLEX__PG_DSN
$pgBroker = $env:BROKER_DATABASE_URL; if (-not $pgBroker -and $env:REFLEX__BROKER_DSN) { $pgBroker = $env:REFLEX__BROKER_DSN }
$fernet   = $env:REFLEX__FERNET_KEY
$redisUrl = $env:GARNET_URL; if (-not $redisUrl) { $redisUrl = "redis://127.0.0.1:6379/0" }
$tiq      = $env:REFLEX__INTENTS_QUEUE; if (-not $tiq) { $tiq = "reflex:live:intents.orders" }
$teq      = $env:REFLEX__EVENTS_QUEUE;  if (-not $teq) { $teq = "reflex:live:events.orders" }
$apiPort  = $env:TRADER_API_PORT; if (-not $apiPort) { $apiPort = 7002 }

Write-Host "`n[ENV] essentials (after loading .env)"
"{0,-24} {1}" -f "REFLEX__PG_DSN:",      ($pgMain   ?? "<unset>")
"{0,-24} {1}" -f "BROKER_DATABASE_URL:", ($pgBroker ?? "<unset>")
"{0,-24} {1}" -f "GARNET_URL:",          $redisUrl
"{0,-24} {1}" -f "TRADER_API_PORT:",     $apiPort
"{0,-24} {1}" -f "FERNET_KEY set?:",     ($(if ($fernet) {"YES"} else {"NO"}))

# 3) Connectivity
Write-Host "`n[PING] Postgres (main)";   psqlc $pgMain   "SELECT 'ok:stock_data' AS ping;"
Write-Host "`n[PING] Postgres (broker)"; psqlc $pgBroker "SELECT 'ok:brokerdb'  AS ping;"
Write-Host "`n[PING] Redis";             redis-cli -u $redisUrl PING

# 4) Broker schema + counts
Write-Host "`n[VERIFY] broker schema tables exist"
psqlc $pgBroker @"
SELECT relname AS table
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND relkind='r' AND relname IN ('brokers','broker_accounts','credentials','orders','fills')
ORDER BY relname;
"@

Write-Host "`n[VERIFY] broker table counts"
psqlc $pgBroker @"
SELECT 'brokers' t, count(*) n FROM brokers
UNION ALL SELECT 'accounts', count(*) FROM broker_accounts
UNION ALL SELECT 'creds',    count(*) FROM credentials
UNION ALL SELECT 'orders',   count(*) FROM orders
UNION ALL SELECT 'fills',    count(*) FROM fills;
"@

Write-Host "`n[VERIFY] accounts by broker"
psqlc $pgBroker @"
SELECT broker_id, array_agg(account_id ORDER BY account_id) AS accounts
FROM broker_accounts GROUP BY broker_id ORDER BY broker_id;
"@

Write-Host "`n[VERIFY] required Alpaca keys present"
psqlc $pgBroker @"
WITH needed AS (
  SELECT * FROM (VALUES
    ('ALPACA_PAPER','api_key_id'),
    ('ALPACA_PAPER','api_secret'),
    ('ALPACA_MARGIN','api_key_id'),
    ('ALPACA_MARGIN','api_secret')
  ) AS v(account_id, key)
)
SELECT n.account_id, n.key,
       CASE WHEN EXISTS (
         SELECT 1 FROM credentials c
         WHERE c.scope_type='account' AND c.scope_id=n.account_id AND c.key=n.key
       ) THEN 'OK' ELSE 'MISSING' END AS status
FROM needed n ORDER BY n.account_id, n.key;
"@

if ($fernet) {
  $mk = $fernet -replace "'","''"
  Write-Host "`n[VERIFY] decrypt-peek (first 8 chars)"
  psqlc $pgBroker @"
SELECT scope_id AS account_id, key,
       CASE WHEN key IN ('api_key_id','api_secret')
            THEN LEFT(pgp_sym_decrypt(value_cipher, '$mk')::text, 8) || '…'
            ELSE NULL END AS prefix
FROM credentials
WHERE scope_type='account' AND scope_id IN ('ALPACA_PAPER','ALPACA_MARGIN')
ORDER BY account_id, key;
"@
} else {
  Write-Host "`n[NOTE] REFLEX__FERNET_KEY not set; skipping decrypt-peek."
}

# 5) Redis queues (non-destructive)
Write-Host "`n[REDIS] queue lengths"
"{0,-28} {1}" -f $tiq, (redis-cli -u $redisUrl LLEN $tiq)
"{0,-28} {1}" -f $teq, (redis-cli -u $redisUrl LLEN $teq)

# 6) Trader health (if running)
Write-Host "`n[HTTP] Trader /health on port $apiPort"
try {
  $resp = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$apiPort/health" -TimeoutSec 2
  "status: $($resp.StatusCode)"; $resp.Content
} catch {
  Write-Warning "Trader not responding on /health (maybe not started yet)."
}

Write-Host "`n[DONE] Verifier complete."
# ================================================
