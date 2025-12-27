
param(
  [ValidateSet("soft","hard")] [string] $Mode = "soft",
  [ValidateSet("Minimized","Normal","Maximized","Hidden")] [string] $WindowStyle = "Minimized",
  [string] $RepoRoot = (Join-Path $PSScriptRoot "..")
)

Write-Host "[BOOT] Windows launcher Mode=$Mode WindowStyle=$WindowStyle"

. (Join-Path $PSScriptRoot "_load_env.ps1") -RepoRoot $RepoRoot

if ($Mode -eq "hard") {
  Write-Host "[NUKE] Redis reflex:* keys"
  if ($env:GARNET_URL) {
    redis-cli -u $env:GARNET_URL KEYS reflex:* | ForEach-Object { redis-cli -u $env:GARNET_URL DEL $_ | Out-Null }
  } else {
    redis-cli KEYS reflex:* | ForEach-Object { redis-cli DEL $_ | Out-Null }
  }
}

function Launch-One {
  param([string]$name)
  $args = @(
    "-NoLogo","-NoExit",
    "-File", (Join-Path $PSScriptRoot "_launch_service_window.ps1"),
    "-Name", $name,
    "-Mode", $Mode,
    "-RepoRoot", $RepoRoot
  )
  Start-Process -FilePath "pwsh" -ArgumentList $args -WorkingDirectory $RepoRoot -WindowStyle $WindowStyle | Out-Null
  Start-Sleep -Milliseconds 400
}

Launch-One "datahub"
Launch-One "evaluator"
Launch-One "trader"
if (Test-Path (Join-Path $RepoRoot "brokerview\app.py")) { Launch-One "brokerview" }
Launch-One "broker_worker"
# Optional tool windows:
# Launch-One "symbols"
# Launch-One "brokers"

Write-Host "[OK] Spawned windows."
