#requires -Version 7.0
param(
  [string]$Root = 'C:\Projects\Reflex2.2',
  [string]$Start,
  [string]$End,
  [switch]$DaysFirst,
  [switch]$IncludeWeekends,
  [string]$PgDsn,
  [string]$PolygonKey,
  [string]$SymbolsSql = "FROM symbol_metadata ORDER BY symbol",
  [ValidateSet('single','split2')][string]$Mode = 'single',
  [double]$SleepBetweenSymbols = 0.05
)

$ErrorActionPreference = 'Stop'
Push-Location $Root
try {
  New-Item -ItemType Directory -Force -Path .\logs | Out-Null

  # --- Load .env (only if vars not already set in the session) ---
  $dotenv = Join-Path $Root ".env"
  if (Test-Path $dotenv) {
    foreach ($line in Get-Content $dotenv) {
      if ($line -match '^\s*#' -or $line -match '^\s*$') { continue }
      $name, $value = $line -split '=', 2
      $name  = $name.Trim()
      $value = $value.Trim().Trim('"')
      if (-not [string]::IsNullOrWhiteSpace($name)) {
        if (-not [Environment]::GetEnvironmentVariable($name,'Process')) { Set-Item "Env:$name" $value }
      }
    }
  }

  # --- Resolve inputs (fallbacks to env / sensible defaults) ---
  if (-not $Start) { $Start = (Get-Date).AddDays(-3).ToString('yyyy-MM-dd') }
  if (-not $End)   { $End   = (Get-Date).ToString('yyyy-MM-dd') }

  if (-not $PgDsn)     { $PgDsn     = $env:REFLEX__PG_DSN }
  if (-not $PolygonKey){ $PolygonKey= $env:POLYGON_API_KEY }

  if (-not $PgDsn)     { throw "Missing Postgres DSN (pass -PgDsn or set REFLEX__PG_DSN in .env)" }
  if (-not $PolygonKey){ throw "Missing Polygon API key (pass -PolygonKey or set POLYGON_API_KEY in .env)" }

  $env:PYTHONUNBUFFERED = '1'
  $env:REFLEX__ROOT     = $Root

  $argsCommon = @(
    '-u','-X','dev','-m','dbmanager.ticks_backfill',
    '--pg-dsn', $PgDsn,
    '--source','polygon',
    '--start',  $Start,
    '--end',    $End,
    '--api-key',$PolygonKey,
    '--sleep-between-symbols', $SleepBetweenSymbols.ToString()
  )
  if ($DaysFirst)      { $argsCommon += @('--order','days-first') } else { $argsCommon += @('--order','symbols-first') }
  if ($IncludeWeekends){ $argsCommon += '--include-weekends' }

  switch ($Mode) {
    'single' {
      $args1 = $argsCommon + @('--symbols-sql', $SymbolsSql)
      Write-Host "Launching ticks (single): $Start → $End"
      .\.venv\Scripts\python.exe @args1 *>&1 | Tee-Object .\logs\ticks_single.live.log
    }
    'split2' {
      $a = $argsCommon + @('--symbols-sql', "FROM symbol_metadata WHERE symbol BETWEEN 'A' AND 'MZZZ' ORDER BY symbol")
      $b = $argsCommon + @('--symbols-sql', "FROM symbol_metadata WHERE symbol >= 'N' ORDER BY symbol")

      Write-Host "Launching ticks (split2) workers…"
      Start-Process .\.venv\Scripts\python.exe -ArgumentList $a -RedirectStandardOutput .\logs\ticks_A.out -RedirectStandardError .\logs\ticks_A.err -WindowStyle Minimized
      Start-Process .\.venv\Scripts\python.exe -ArgumentList $b -RedirectStandardOutput .\logs\ticks_B.out -RedirectStandardError .\logs\ticks_B.err -WindowStyle Minimized
      Write-Host "Tail logs with:`n  Get-Content .\logs\ticks_A.out -Wait`n  Get-Content .\logs\ticks_B.out -Wait"
    }
  }
}
finally {
  Pop-Location
}
