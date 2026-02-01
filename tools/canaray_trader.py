'''Basic canaray trader API tester.'''
''' C:\Projects\Reflex2.5\tools\canary_trader.py http://127.0.0.1:7002 alpaca:paper
'''


import json
import sys
import urllib.request
import urllib.error
from urllib.parse import quote

TRADER_BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7002"
ACCOUNT_ID  = sys.argv[2] if len(sys.argv) > 2 else "alpaca:paper"

def hit(label: str, method: str, path: str, body: dict | None = None, expect_any=(200, 422)):
    url = TRADER_BASE + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            txt = resp.read().decode("utf-8", errors="replace")
            ok = resp.status in expect_any
            return {"label": label, "method": method, "url": url, "status": resp.status, "ok": ok, "body": txt[:400]}
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", errors="replace")
        ok = e.code in expect_any
        return {"label": label, "method": method, "url": url, "status": e.code, "ok": ok, "body": txt[:400]}
    except Exception as e:
        return {"label": label, "method": method, "url": url, "status": -1, "ok": False, "body": str(e)}

tests = [
    ("health", "GET",  "/v1/health", None, (200,)),
    ("time",   "GET",  "/v1/time", None, (200,)),
    ("acct",   "GET",  "/v1/accounts", None, (200,)),
    ("ovw",    "GET",  "/v1/portfolio/overview", None, (200,)),
    ("pos",    "GET",  f"/v1/portfolio/positions?account_id={quote(ACCOUNT_ID)}", None, (200,)),
    ("ordA",   "GET",  f"/v1/orders?account_id={quote(ACCOUNT_ID)}&status=active", None, (200,)),
    ("ordC",   "GET",  f"/v1/orders?account_id={quote(ACCOUNT_ID)}&status=closed", None, (200,)),
    ("events", "GET",  "/v1/events", None, (200,)),
    # route existence: 422 is success here
    ("placeRoute", "POST", "/v1/orders/place", {}, (422, 200)),
]

results = []
for (label, method, path, body, expect) in tests:
    r = hit(label, method, path, body, expect_any=expect)
    results.append(r)
    status = "PASS" if r["ok"] else "FAIL"
    print(f"{status:4} {label:10} {r['status']:4} {method:4} {path}")

bad = [r for r in results if not r["ok"]]
if bad:
    print("\n--- failures ---")
    print(json.dumps(bad, indent=2)[:4000])
    sys.exit(1)

print("\nOK")
sys.exit(0)
