
param(
  [string] $RepoRoot = (Join-Path $PSScriptRoot "..")
)

$envPath = Join-Path $RepoRoot ".env"
if (Test-Path $envPath) {
  Get-Content $envPath | ForEach-Object {
    if ($_ -match '^\s*#') { return }
    if ($_ -match '^\s*$') { return }
    $k,$v = $_.Split('=',2)
    if ($k -and $v) { [System.Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim()) }
  }
}
$env:PYTHONPATH = $RepoRoot
$pyVenv = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (Test-Path $pyVenv) { $env:REFLEX__PY = $pyVenv } else { $env:REFLEX__PY = "python" }
