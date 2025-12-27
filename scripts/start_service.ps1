param(
  [Parameter(Mandatory=$true)]
  [ValidateSet("datahub","evaluator","trader","brokerview","broker_worker","symbols")] [string] $Name,
  [ValidateSet("soft","hard")] [string] $Mode = "soft"
)

# Load .env
$envPath = Join-Path $PSScriptRoot "..\.env"
if (Test-Path $envPath) {
  Get-Content $envPath | ForEach-Object {
    if ($_ -match '^\s*#') { return }
    if ($_ -match '^\s*$') { return }
    $k,$v = $_.Split('=',2)
    if ($k -and $v) { [System.Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim()) }
  }
}

# Optional hard clean per-service
if ($Mode -eq "hard") {
  if ($Name -in @("trader","broker_worker")) {
    Write-Host "[NUKE] Clearing order queues (reflex:*orders*)"
    if ($env:GARNET_URL) {
      redis-cli -u $env:GARNET_URL KEYS "reflex:*orders*" | ForEach-Object { redis-cli -u $env:GARNET_URL DEL $_ | Out-Null }
      redis-cli -u $env:GARNET_URL KEYS "reflex:*intents*" | ForEach-Object { redis-cli -u $env:GARNET_URL DEL $_ | Out-Null }
    } else {
      redis-cli KEYS "reflex:*orders*" | ForEach-Object { redis-cli DEL $_ | Out-Null }
      redis-cli KEYS "reflex:*intents*" | ForEach-Object { redis-cli DEL $_ | Out-Null }
    }
  }
}

$PY = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
$env:PYTHONPATH = (Join-Path $PSScriptRoot "..")

$module = switch ($Name) {
  "datahub"       { "datahub.app" }
  "evaluator"     { "evaluator.app" }
  "trader"        { "trader.app" }
  "brokerview"    { "brokerview.app" }
  "broker_worker" { "trader.broker_worker" }
  "symbols"       { "tools.symbol_manager.cli" }
  "brokers"       { "tools.broker_manager.cli" }  # <-- use YOUR broker_manager editor
}

Write-Host "[START] $Name -> $module"
& $PY -m $module
