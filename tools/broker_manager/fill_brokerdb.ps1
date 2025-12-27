# fill_brokerdb.ps1 — one-shot broker DB init + verify (self-contained, no kludges)
# - Locates <repo root>\.env reliably (two levels up)
# - Reads DB DSN + REFLEX__FERNET_KEY from .env
# - Applies schema.sql (with UNIQUE on credentials for ON CONFLICT)
# - Seeds brokers + accounts (idempotent)
# - Writes Alpaca creds from inline constants using pgcrypto (api_key_id/api_secret/base_url)
# - Verifies counts, mappings, and decrypt-peek

$ErrorActionPreference = 'Stop'

# --------------------- Inline Alpaca credentials (single-shot) ---------------
$ALPACA_PAPER_KEY_ID = "PKAX36SSTH4CF5FY5GYBYIKB2K"
$ALPACA_PAPER_SECRET = "H45gfUDGEVW66mpgQ3u7wppiDog3t2E6KpEdNrY12FQR"
$ALPACA_PAPER_BASE   = "https://paper-api.alpaca.markets"

$ALPACA_LIVE_KEY_ID  = "AKZRQQKRVOJE5T37A3WKQDJTGD"
$ALPACA_LIVE_SECRET  = "2H3E3UyxuSoif9TS9yEC2c3zPkpmQRAtBpwRTf314NEX"
$ALPACA_LIVE_BASE    = "https://api.alpaca.markets"
# -----------------------------------------------------------------------------

# --------------------------------- Helpers -----------------------------------
function SqlQ      { param([string]$s) if ($null -eq $s) { '' } else { $s -replace "'","''" } }
function PsqlExec  { param([string]$dsn,[string]$sql)
  if(-not $dsn){ throw "PsqlExec: DSN missing" }
  & psql $dsn -v ON_ERROR_STOP=1 -X -q -c $sql
}
function PsqlFile  { param([string]$dsn,[string]$path)
  if(-not $dsn){ throw "PsqlFile: DSN missing" }
  & psql $dsn -v ON_ERROR_STOP=1 -X -f $path
}
function PsqlScalar{ param([string]$dsn,[string]$sql)
  if(-not $dsn){ throw "PsqlScalar: DSN missing" }
  $val = & psql $dsn -t -A -X -q -c $sql
  return ($val.Trim())
}
function Import-DotEnv-File {
  param([string]$Path)
  if (!(Test-Path -LiteralPath $Path)) { throw ".env not found at $Path" }
  Get-Content -LiteralPath $Path | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { return }
    $eq = $line.IndexOf("="); if ($eq -lt 1) { return }
    $k = $line.Substring(0,$eq).Trim()
    $v = $line.Substring($eq+1)
    if     ($v -match '^\s*"(.*)"\s*$') { $v = $matches[1] }
    elseif ($v -match "^\s*'(.*)'\s*$") { $v = $matches[1] }
    Set-Item -Path "Env:$k" -Value $v
  }
}
# -----------------------------------------------------------------------------

# --- Paths (robust): repo root is two levels up from this script
$scriptDir = Split-Path -Parent $PSCommandPath                    # ...\tools\broker_manager
$repoRoot  = (Resolve-Path (Join-Path $scriptDir "..\..")).Path   # ...\Reflex2.2
$envPath   = Join-Path $repoRoot ".env"
$schemaPath= Join-Path $scriptDir "sql\schema.sql"

Write-Host "[PATH] scriptDir=$scriptDir"
Write-Host "[PATH] repoRoot=$repoRoot"
Write-Host "[PATH] .env=$envPath"
Write-Host "[PATH] schema=$schemaPath"

# --- Load .env (DB DSN + Fernet)
Import-DotEnv-File -Path $envPath

$PGURL  = $env:BROKER_DATABASE_URL
if (-not $PGURL -and $env:REFLEX__BROKER_DSN) { $PGURL = $env:REFLEX__BROKER_DSN }
if (-not $PGURL)  { throw "Need BROKER_DATABASE_URL or REFLEX__BROKER_DSN in .env" }

$MASTER = $env:REFLEX__FERNET_KEY
if (-not $MASTER) { throw "Need REFLEX__FERNET_KEY in .env for pgcrypto encryption" }
$MASTERQ = SqlQ $MASTER

# --- Create database if missing (parse DB from DSN)
if ($PGURL -notmatch "/([^/]+)$") { throw "Could not parse DB name from DSN: $PGURL" }
$DBNAME   = $Matches[1]
$AdminDSN = $PGURL -replace "/$DBNAME$", "/postgres"

Write-Host "[CHECK] database '$DBNAME'"
$exists = PsqlScalar $AdminDSN "SELECT COUNT(*) FROM pg_database WHERE datname='${DBNAME}';"
if ($exists -ne "1") {
  Write-Host "[CREATE] database $DBNAME"
  PsqlExec $AdminDSN "CREATE DATABASE ${DBNAME};"
}

# --- Schema & extension (idempotent) (includes UNIQUE constraint)
Write-Host "[SCHEMA] ensure pgcrypto + tables"
PsqlExec $PGURL "CREATE EXTENSION IF NOT EXISTS pgcrypto;"
PsqlFile $PGURL $schemaPath

# --- Seed brokers (idempotent)
PsqlExec $PGURL @"
INSERT INTO brokers (broker_id, display_name, api_base, kind) VALUES
 ('ALPACA','Alpaca',NULL,'equities'),
 ('SIM','Internal Simulator',NULL,'sim'),
 ('WEBULL','Webull',NULL,'equities'),
 ('SCHWAB','Charles Schwab',NULL,'equities'),
 ('IBKR','Interactive Brokers',NULL,'equities'),
 ('LIGHTSPEED','Lightspeed',NULL,'equities')
ON CONFLICT (broker_id) DO UPDATE
  SET display_name=EXCLUDED.display_name,
      api_base=EXCLUDED.api_base,
      kind=EXCLUDED.kind;
"@

# --- Seed accounts (idempotent; minimal cols; no ints/JSON pitfalls)
PsqlExec $PGURL @"
INSERT INTO broker_accounts (account_id, broker_id, account_code, label, margin, paper) VALUES
 ('ALPACA_PAPER','ALPACA','paper','Alpaca Paper',FALSE,TRUE),
 ('ALPACA_MARGIN','ALPACA','live','Alpaca Live Margin',TRUE,FALSE),
 ('SIM_CASH','SIM','sim','Simulator Cash',FALSE,FALSE),
 ('SIM_MARGIN','SIM','sim','Simulator Margin',TRUE,FALSE)
ON CONFLICT (account_id) DO UPDATE
  SET broker_id=EXCLUDED.broker_id,
      account_code=EXCLUDED.account_code,
      label=EXCLUDED.label,
      margin=EXCLUDED.margin,
      paper=EXCLUDED.paper;
"@

# --- Ensure ON CONFLICT target exists for credentials (safety)
PsqlExec $PGURL @"
DO \$\$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname='uq_credentials_scope'
  ) THEN
    ALTER TABLE credentials
      ADD CONSTRAINT uq_credentials_scope
      UNIQUE (scope_type, scope_id, key);
  END IF;
END \$\$;
"@

# --- Clean + upsert encrypted credentials using canonical key names
$paper_keyQ = SqlQ $ALPACA_PAPER_KEY_ID
$paper_secQ = SqlQ $ALPACA_PAPER_SECRET
$paper_baseQ= SqlQ $ALPACA_PAPER_BASE
$live_keyQ  = SqlQ $ALPACA_LIVE_KEY_ID
$live_secQ  = SqlQ $ALPACA_LIVE_SECRET
$live_baseQ = SqlQ $ALPACA_LIVE_BASE

PsqlExec $PGURL @"
DELETE FROM credentials
WHERE scope_type='account'
  AND scope_id IN ('ALPACA_PAPER','ALPACA_MARGIN')
  AND key IN ('api_key_id','api_secret','base_url');
"@

PsqlExec $PGURL @"
INSERT INTO credentials (scope_type, scope_id, key, value_cipher) VALUES
 ('account','ALPACA_PAPER','api_key_id', pgp_sym_encrypt('$paper_keyQ',  '$MASTERQ','cipher-algo=aes256')),
 ('account','ALPACA_PAPER','api_secret', pgp_sym_encrypt('$paper_secQ',  '$MASTERQ','cipher-algo=aes256')),
 ('account','ALPACA_PAPER','base_url',   pgp_sym_encrypt('$paper_baseQ', '$MASTERQ','cipher-algo=aes256')),
 ('account','ALPACA_MARGIN','api_key_id', pgp_sym_encrypt('$live_keyQ',  '$MASTERQ','cipher-algo=aes256')),
 ('account','ALPACA_MARGIN','api_secret', pgp_sym_encrypt('$live_secQ',  '$MASTERQ','cipher-algo=aes256')),
 ('account','ALPACA_MARGIN','base_url',   pgp_sym_encrypt('$live_baseQ', '$MASTERQ','cipher-algo=aes256'))
ON CONFLICT (scope_type, scope_id, key) DO UPDATE
  SET value_cipher = EXCLUDED.value_cipher;
"@

# ------------------------------ Verify ---------------------------------------
Write-Host "`n[VERIFY] counts"
PsqlExec $PGURL @"
SELECT 'brokers' t, count(*) n FROM brokers
UNION ALL SELECT 'accounts', count(*) FROM broker_accounts
UNION ALL SELECT 'creds',    count(*) FROM credentials;
"@

Write-Host "`n[VERIFY] accounts by broker"
PsqlExec $PGURL @"
SELECT broker_id, array_agg(account_id ORDER BY account_id) AS accounts
FROM broker_accounts GROUP BY broker_id ORDER BY broker_id;
"@

Write-Host "`n[VERIFY] Alpaca credential keys (names only)"
PsqlExec $PGURL @"
SELECT scope_id AS account_id, key
FROM credentials
WHERE scope_type='account' AND scope_id IN ('ALPACA_PAPER','ALPACA_MARGIN')
ORDER BY account_id, key;
"@

Write-Host "`n[VERIFY] decrypt peek (first 8 chars)"
PsqlExec $PGURL @"
SELECT scope_id AS account_id, key,
       CASE WHEN key IN ('api_key_id','api_secret')
            THEN LEFT(pgp_sym_decrypt(value_cipher, '$MASTERQ')::text, 8) || '…'
            ELSE NULL
       END AS prefix
FROM credentials
WHERE scope_type='account' AND scope_id IN ('ALPACA_PAPER','ALPACA_MARGIN')
ORDER BY account_id, key;
"@

Write-Host "`n[DONE] One-shot broker DB initialized."
