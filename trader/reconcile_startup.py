"""
trader/reconcile_startup.py
Broker-is-truth reconciliation for Reflex.

What it does
------------
- Pulls broker snapshot: open orders, positions, account (cash, BP, PDT, etc.)
- Peeks local inflight intents from Redis (configurable list keys)
- Computes diffs, deadletters local ghosts, optional auto-cancel broker strays
- Optionally sets a circuit-breaker halt if mismatches are found
- Publishes a 'reconcile_report' on reflex:events.system
- Prints a JSON report to stdout when executed as a script

Env vars
--------
GARNET_URL                         (default: redis://127.0.0.1:6379)
ALPACA_BASE                        (default: https://paper-api.alpaca.markets)
ALPACA_API_KEY_ID
ALPACA_API_SECRET_KEY
REFLEX__INTENT_KEYS                (comma list, default: reflex:intents:auto,reflex:intents.list)
REFLEX__HALT_ON_RECONCILE_DIFF     (1/0, default: 1)
REFLEX__CANCEL_BROKER_STRAYS       (1/0, default: 0)
"""

from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Tuple

import redis
import requests


GARNET_URL = os.getenv("GARNET_URL", "redis://127.0.0.1:6379")
ALPACA_BASE = os.getenv("ALPACA_BASE", "https://paper-api.alpaca.markets")
ALPACA_KEY = os.getenv("ALPACA_API_KEY_ID", "")
ALPACA_SEC = os.getenv("ALPACA_API_SECRET_KEY", "")

HALT_KEY = "reflex:trading:halt"
HALT_REASON = "reflex:trading:halt_reason"

INTENT_KEYS_DEFAULT = ["reflex:intents:auto", "reflex:intents.list"]


def rconn() -> redis.Redis:
    return redis.Redis.from_url(GARNET_URL, decode_responses=True)


def alpaca_headers() -> Dict[str, str]:
    if not (ALPACA_KEY and ALPACA_SEC):
        raise RuntimeError("Missing ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY")
    return {
        "APCA-API-KEY-ID": ALPACA_KEY.strip(),
        "APCA-API-SECRET-KEY": ALPACA_SEC.strip(),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def broker_open_orders() -> List[Dict]:
    r = requests.get(
        f"{ALPACA_BASE}/v2/orders",
        headers=alpaca_headers(),
        params={"status": "open", "limit": 500},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def broker_positions() -> List[Dict]:
    r = requests.get(f"{ALPACA_BASE}/v2/positions", headers=alpaca_headers(), timeout=15)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return r.json()


def broker_account() -> Dict:
    r = requests.get(f"{ALPACA_BASE}/v2/account", headers=alpaca_headers(), timeout=15)
    r.raise_for_status()
    return r.json()


def get_local_intents(r: redis.Redis, keys: List[str], max_peek: int = 500) -> List[Dict]:
    """Non-destructive peek into configured intent lists."""
    out: List[Dict] = []
    for key in keys:
        n = r.llen(key)
        if n <= 0:
            continue
        peek_n = min(max_peek, n)
        for raw in r.lrange(key, 0, peek_n - 1):
            try:
                out.append(json.loads(raw))
            except Exception:
                # Keep malformed payloads visible
                out.append({"_raw": raw})
    return out


def key_from_orderlike(o: Dict) -> Tuple[str, str]:
    """Prefer client_order_id; else derive a stable signature."""
    cid = (o.get("client_order_id") or o.get("clientOrderId") or "").strip() if isinstance(o.get("client_order_id") or o.get("clientOrderId"), str) else o.get("client_order_id") or o.get("clientOrderId")
    if cid:
        return ("cid", str(cid))
    sig = f"{o.get('symbol')}|{o.get('side')}|{o.get('qty')}|{o.get('type')}|{o.get('limit_price')}"
    return ("sig", sig)


def diff_orders(broker: List[Dict], local: List[Dict]) -> Tuple[List[Dict], List[Dict], List[Tuple[Dict, Dict]]]:
    bro_idx: Dict[Tuple[str, str], Dict] = {}
    for b in broker:
        bro_idx[key_from_orderlike(b)] = b

    loc_idx: Dict[Tuple[str, str], Dict] = {}
    for l in local:
        loc_idx[key_from_orderlike(l)] = l

    stray_on_broker = [bro_idx[k] for k in bro_idx.keys() - loc_idx.keys()]
    stray_on_local = [loc_idx[k] for k in loc_idx.keys() - bro_idx.keys()]
    overlaps = [(loc_idx[k], bro_idx[k]) for k in bro_idx.keys() & loc_idx.keys()]
    return stray_on_broker, stray_on_local, overlaps


def deadletter_local_strays(r: redis.Redis, items: List[Dict]) -> str:
    day = time.strftime("%Y%m%d")
    dlkey = f"reflex:deadletter:{day}:startup_reconcile"
    for item in items:
        r.rpush(dlkey, json.dumps({"reason": "local_stray_no_broker_match", "intent": item}))
    return dlkey


def maybe_cancel_broker_strays(strays: List[Dict]) -> List[Dict]:
    """Optional auto-cancel at broker (opt-in). Returns results with success flag."""
    results = []
    if os.getenv("REFLEX__CANCEL_BROKER_STRAYS", "0").lower() not in ("1", "true", "yes"):
        return results
    for b in strays:
        oid = b.get("id")
        if not oid:
            continue
        try:
            r = requests.delete(f"{ALPACA_BASE}/v2/orders/{oid}", headers=alpaca_headers(), timeout=10)
            ok = r.status_code in (200, 204)
            results.append({"order_id": oid, "cancel_ok": ok, "status_code": r.status_code})
        except Exception as e:
            results.append({"order_id": oid, "cancel_ok": False, "error": str(e)})
    return results


def set_halt(r: redis.Redis, reason: str) -> None:
    r.set(HALT_KEY, "1")
    r.set(HALT_REASON, reason)


def clear_halt(r: redis.Redis) -> None:
    r.delete(HALT_KEY)
    r.delete(HALT_REASON)


def reconcile() -> Dict:
    rdb = rconn()

    intent_keys = [k.strip() for k in os.getenv("REFLEX_INTENT_KEYS", "").split(",") if k.strip()] or INTENT_KEYS_DEFAULT

    bro_orders = broker_open_orders()
    bro_pos = broker_positions()
    acct = broker_account()
    loc_inflight = get_local_intents(rdb, intent_keys)

    s_broker, s_local, overlaps = diff_orders(bro_orders, loc_inflight)

    dlkey = None
    if s_local:
        dlkey = deadletter_local_strays(rdb, s_local)

    cancel_results = maybe_cancel_broker_strays(s_broker)

    report = {
        "broker_open_orders": len(bro_orders),
        "broker_positions": len(bro_pos),
        "local_inflight_peek": len(loc_inflight),
        "stray_on_broker": len(s_broker),
        "stray_on_local": len(s_local),
        "overlaps": len(overlaps),
        "deadletter_key": dlkey,
        "broker_stray_cancels": cancel_results or None,
        "account": {
            "cash": acct.get("cash"),
            "buying_power": acct.get("buying_power"),
            "portfolio_value": acct.get("portfolio_value"),
            "daytrade_count": acct.get("daytrade_count"),
            "pattern_day_trader": acct.get("pattern_day_trader"),
            "multiplier": acct.get("multiplier"),
            "shorting_enabled": acct.get("shorting_enabled"),
            "account_blocked": acct.get("account_blocked"),
        },
        "examples": {
            "stray_on_broker": s_broker[:3],
            "stray_on_local": s_local[:3],
            "overlap_first": overlaps[0] if overlaps else None,
        },
    }

    # Circuit breaker if anything is mismatched
    if os.getenv("REFLEX_HALT_ON_RECONCILE_DIFF", "1").lower() in ("1", "true", "yes"):
        if report["stray_on_broker"] or report["stray_on_local"]:
            set_halt(rdb, f"startup_reconcile_mismatch:{report['stray_on_broker']}|{report['stray_on_local']}")

    # Broadcast for cockpit
    try:
        rdb.publish("reflex:events.system", json.dumps({"type": "reconcile_report", "report": report}))
    except Exception:
        pass

    return report


if __name__ == "__main__":
    rep = reconcile()
    print(json.dumps(rep, indent=2))
