param(
  [ValidateSet("soft","hard")] [string] $Mode = "soft"
)

# Load .env into current process
$envPath = Join-Path $PSScriptRoot "..\.env"
if (Test-Path $envPath) {
  Get-Content $envPath | ForEach-Object {
    if ($_ -match '^\s*#') { return }
    if ($_ -match '^\s*$') { return }
    $k,$v = $_.Split('=',2)
    if ($k -and $v) { [System.Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim()) }
  }
}

Write-Host "[BOOT] Mode=$Mode"

# Optional hard clean
if ($Mode -eq "hard") {
  Write-Host "[NUKE] Redis reflex:* keys"
  if ($env:GARNET_URL) {
    redis-cli -u $env:GARNET_URL KEYS reflex:* | ForEach-Object { redis-cli -u $env:GARNET_URL DEL $_ | Out-Null }
  } else {
    redis-cli KEYS reflex:* | ForEach-Object { redis-cli DEL $_ | Out-Null }
  }
}

# Start services in order: datahub -> evaluator -> trader -> brokerview (if present)
$PY = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
$env:PYTHONPATH = (Join-Path $PSScriptRoot "..")

$procs = @()

function Start-One($name, $module, $args) {
  Write-Host "[START] $name  ($module $args)"
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $PY
  $psi.Arguments = "-m $module $args"
  $psi.WorkingDirectory = (Join-Path $PSScriptRoot "..")
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError  = $true
  $psi.UseShellExecute = $false
  $p = New-Object System.Diagnostics.Process
  $p.StartInfo = $psi
  $p.Start() | Out-Null
  $procs += @{ Name=$name; Proc=$p }
}

Start-One "DataHub" "datahub.app" ""
Start-Sleep -Seconds 2
Start-One "Evaluator" "evaluator.app" ""
Start-Sleep -Seconds 2
Start-One "Trader" "trader.app" ""
Start-Sleep -Seconds 2
if (Test-Path (Join-Path $PSScriptRoot "..\brokerview\app.py")) {
  Start-One "BrokerView" "brokerview.app" ""
}

Write-Host "[OK] Services launched. Use CTRL+C to stop this wrapper."
# Wait loop to mirror outputs
while ($true) {
  foreach ($item in $procs) {
    while (-not $item.Proc.HasExited -and -not $item.Proc.StandardOutput.EndOfStream) {
      $line = $item.Proc.StandardOutput.ReadLine()
      if ($line) { Write-Host ("[{0}] {1}" -f $item.Name, $line) }
    }
    while (-not $item.Proc.HasExited -and -not $item.Proc.StandardError.EndOfStream) {
      $eline = $item.Proc.StandardError.ReadLine()
      if ($eline) { Write-Host ("[{0}][ERR] {1}" -f $item.Name, $eline) -ForegroundColor Red }
    }
  }
  Start-Sleep -Milliseconds 300
}
