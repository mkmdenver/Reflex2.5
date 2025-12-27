$env:PYTHONPATH = "$PWD"
Start-Process -NoNewWindow "$PWD\.venv\Scripts\python.exe" -ArgumentList "trader\risk_sync.py"
& "$PWD\.venv\Scripts\python.exe" - << 'PYCODE'
import asyncio, os
from trader.ipc_consume import trader_loop
asyncio.run(trader_loop())
PYCODE
