# trader/reconcile_trigger.py
from __future__ import annotations
import os, json, time
from typing import Iterable, List
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from common import logging as log

COMPONENT = "trader.reconcile_trigger"

def _resolve_base_url() -> str:
    # Prefer explicit URL; else build from port
    base = os.getenv("TRADER_API_URL")
    if base:
        return base.rstrip("/")
    port = os.getenv("TRADER_API_PORT") or "7002"
    return f"http://127.0.0.1:{port}"

def trigger_reconcile(accounts: Iterable[str], max_retries: int = 3, backoff_s: float = 0.15) -> bool:
    """
    Fire-and-forget HTTP POST to Trader API /v1/portfolio/reconcile with the given accounts.
    Returns True on 2xx; False otherwise. Retries a couple of times with tiny backoff.
    """
    accs: List[str] = [a for a in (accounts or []) if a]
    if not accs:
        return False

    base = _resolve_base_url()
    url = f"{base}/v1/portfolio/reconcile"
    body = json.dumps({"accounts": accs}).encode("utf-8")
    req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")

    for attempt in range(1, max_retries + 1):
        try:
            with urlopen(req, timeout=1.5) as resp:
                ok = 200 <= resp.status < 300
                if ok:
                    log.info(COMPONENT, "trigger.ok", accounts=",".join(accs), attempt=attempt)
                    return True
                log.warn(COMPONENT, "trigger.bad_status", status=resp.status, attempt=attempt)
        except (HTTPError, URLError, TimeoutError) as e:
            log.warn(COMPONENT, "trigger.error", err=str(e), attempt=attempt)
        time.sleep(backoff_s * attempt)  # linear backoff

    return False
