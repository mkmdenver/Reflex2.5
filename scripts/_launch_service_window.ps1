
param(
  [Parameter(Mandatory=$true)]
  [ValidateSet("datahub","evaluator","trader","brokerview","broker_worker","symbols","brokers")] [string] $Name,
  [ValidateSet("soft","hard")] [string] $Mode = "soft",
  [string] $RepoRoot = (Join-Path $PSScriptRoot "..")
)

. (Join-Path $PSScriptRoot "_load_env.ps1") -RepoRoot $RepoRoot

if ($Mode -eq "hard" -and ($Name -in @("trader","broker_worker"))) {
  Write-Host "[NUKE] Clearing order queues (reflex:*orders* and reflex:*intents*)"
  if ($env:GARNET_URL) {
    redis-cli -u $env:GARNET_URL KEYS "reflex:*orders*"  | ForEach-Object { redis-cli -u $env:GARNET_URL DEL $_ | Out-Null }
    redis-cli -u $env:GARNET_URL KEYS "reflex:*intents*" | ForEach-Object { redis-cli -u $env:GARNET_URL DEL $_ | Out-Null }
  } else {
    redis-cli KEYS "reflex:*orders*"  | ForEach-Object { redis-cli DEL $_ | Out-Null }
    redis-cli KEYS "reflex:*intents*" | ForEach-Object { redis-cli DEL $_ | Out-Null }
  }
}

$module = switch ($Name) {
  "datahub"       { "datahub.app" }
  "evaluator"     { "evaluator.app" }
  "trader"        { "trader.app" }
  "brokerview"    { "brokerview.app" }
  "broker_worker" { "trader.broker_worker" }
  "symbols"       { "tools.symbol_manager.cli" }
  "brokers"       { "tools.broker_manager.cli" }
}

try { $host.UI.RawUI.WindowTitle = ("Reflex: " + $Name.ToUpper()) } catch {}
Set-Location $RepoRoot
$py = $env:REFLEX__PY
Write-Host "[START] $Name  ($module)"
& $py -m $module
