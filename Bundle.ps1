<#  Save-ReflexBundle.ps1
    Creates a clean ZIP of the Reflex repo for code review.
    - Excludes: .venv, __pycache__, logs, parquet/data, node_modules, .git, secrets, etc.
    - Keeps: source code, configs, templates, requirements/pyproject, scripts.

    Usage:
      pwsh -ExecutionPolicy Bypass -File .\Save-ReflexBundle.ps1
      pwsh .\Save-ReflexBundle.ps1 -OutName "reflex-2.2-prep.zip"
#>

param(
  [string]$OutName = $( "reflex2_bundle_{0:yyyyMMdd_HHmmss}.zip" -f (Get-Date) )
)

$ErrorActionPreference = "Stop"

# --- repo root (where this script lives) ---
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# --- where we stage the filtered copy ---
$Stage = Join-Path $env:TEMP ("reflex_stage_" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $Stage | Out-Null

# --- dirs likely to contain code we want (add if present) ---
$CandidateDirs = @(
  "common","datahub","evaluator","trader","cockpit",
  "tools","scripts","docs"
) | Where-Object { Test-Path (Join-Path $Root $_) }




$KeepRootFiles = @(
  "README.md","README.txt","LICENSE","pyproject.toml","poetry.lock","requirements.txt",
  "requirements-dev.txt","setup.cfg","setup.py",".editorconfig",".flake8",".ruff.toml",
  ".gitignore",".gitattributes",".env.example","Procfile"
) | Where-Object { Test-Path (Join-Path $Root $_) }

# --- Also include any batch files in root ---
$BatchFiles = Get-ChildItem -LiteralPath $Root -Filter "*.bat" -File -ErrorAction SilentlyContinue |
  Select-Object -ExpandProperty Name

$KeepRootFiles = $KeepRootFiles + $BatchFiles

# --- Exclusions ---
$ExcludeDirs = @(
  ".git",".venv","venv","env","__pycache__",".pytest_cache",".mypy_cache",".ruff_cache",
  "dist","build","node_modules",".idea",".vscode","logs","log","output","artifacts",
  "coverage",".cache","site","htmlcov",".next",".tox",".DS_Store","__pypackages__",
  "parquet","parquets","parquet_data","data","datasets","D:\reflex_parquet"
)

$ExcludeFileGlobs = @(
  "*.log","*.tmp","*.parquet","*.feather","*.orc","*.arrow","*.csv","*.tsv",
  "*.sqlite","*.db","*.pkl","*.joblib","*.ckpt","*.bin","*.pt","*.h5",
  "*.pem","*.pfx","*.key","*.crt",".env",".secrets","secrets.json","*.7z","*.zip"
)

# Helper: robocopy mirroring with exclusions
function Copy-Dir {
  param([string]$Src,[string]$Dst)
  New-Item -ItemType Directory -Path $Dst -Force | Out-Null
  $xd = $ExcludeDirs | ForEach-Object { '/XD', (Join-Path $Src $_) }
  $xf = $ExcludeFileGlobs | ForEach-Object { '/XF', $_ }
  $args = @($Src,$Dst,'/MIR','/R:1','/W:1','/NFL','/NDL','/NJH','/NJS','/NP') + $xd + $xf
  robocopy @args | Out-Null
}

# 1) Copy selected directories (if they exist)
foreach ($d in $CandidateDirs) {
  Copy-Dir -Src (Join-Path $Root $d) -Dst (Join-Path $Stage $d)
}

# 2) Copy root files
foreach ($f in $KeepRootFiles) {
  $dst = Join-Path $Stage $f
  New-Item -ItemType Directory -Path (Split-Path $dst -Parent) -Force | Out-Null
  Copy-Item -LiteralPath (Join-Path $Root $f) -Destination $dst -Force
}

# 3) Also copy any *.toml / *.yaml / *.yml configs in root or config/
Get-ChildItem -LiteralPath $Root -Depth 1 -Include *.toml,*.yaml,*.yml -File -ErrorAction SilentlyContinue |
  ForEach-Object {
    $dst = Join-Path $Stage $_.Name
    Copy-Item -LiteralPath $_.FullName -Destination $dst -Force
  }
if (Test-Path (Join-Path $Root "config")) {
  Copy-Dir -Src (Join-Path $Root "config") -Dst (Join-Path $Stage "config")
}

# 4) Zip it
$ZipPath = Join-Path $Root $OutName
if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
Compress-Archive -Path (Join-Path $Stage '*') -DestinationPath $ZipPath -Force

# 5) Report stats + hash
$fi = Get-Item $ZipPath
$hash = (Get-FileHash $ZipPath -Algorithm SHA256).Hash
"{0}  {1:N2} MB  SHA256={2}" -f ($fi.Name), ($fi.Length/1MB), $hash

# 6) Cleanup stage
Remove-Item -Recurse -Force $Stage
