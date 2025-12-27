# From your repo root
$files = Get-ChildItem -Recurse -File
foreach ($f in $files) {
  Remove-Item -Path $f.FullName -Stream Zone.Identifier -ErrorAction SilentlyContinue
}

