$env:PYTHONPATH = "$PWD"
Start-Process -NoNewWindow "$PWD\.venv\Scripts\python.exe" -ArgumentList "traderisk_sync.py"
& "$PWD\.venv\Scripts\python.exe" "trader\consume_intents.py"
