param(
  [Parameter(Mandatory=$true)][string]$EnvPath
)
$ErrorActionPreference = "Stop"
Write-Host "Loading $EnvPath"
Get-Content -Path $EnvPath | ForEach-Object {
  if ($_ -match '^\s*$' -or $_ -match '^\s*#') { return }
  $k,$v = $_.Split('=',2)
  [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim())
}

$inst = $env:REFLEX__INSTANCE_ID
$newLogDir = "logs\$inst"
if (!(Test-Path $newLogDir)) { New-Item -ItemType Directory -Path $newLogDir | Out-Null }

Start-Process -WindowStyle Normal -FilePath ".\.venv\Scripts\python.exe" `
  -ArgumentList "-m","datahub.worker" `
  -RedirectStandardOutput "$newLogDir\hub.out" -RedirectStandardError "$newLogDir\hub.err"

Start-Sleep -Seconds 2

Start-Process -WindowStyle Normal -FilePath ".\.venv\Scripts\python.exe" `
  -ArgumentList "-m","evaluator.service" `
  -RedirectStandardOutput "$newLogDir\eval.out" -RedirectStandardError "$newLogDir\eval.err"

Write-Host "Launched instance $inst. Logs in $newLogDir"
