# trader/db_brokers.py
from __future__ import annotations
import base64
from dataclasses import dataclass
from typing import Dict, Optional, List
import psycopg
from cryptography.fernet import Fernet, InvalidToken

from .core import get_broker_dsn, get_logger, kvlog, get_fernet_key

log = get_logger("trader.db")

@dataclass
class Broker:
    broker_id: str
    display_name: str
    kind: str
    api_base: Optional[str]
    enabled: bool

@dataclass
class Account:
    account_id: str
    broker_id: str
    label: str
    account_code: Optional[str]
    margin: bool

def _connect():
    dsn = get_broker_dsn()
    log.debug("db.connect dsn=%r", dsn)
    return psycopg.connect(dsn, autocommit=True)

def list_brokers() -> List[Broker]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT broker_id, display_name, kind, api_base, enabled
            FROM brokers
            ORDER BY broker_id
        """)
        rows = cur.fetchall()
    brokers = [Broker(*r) for r in rows]
    log.debug("db.list_brokers count=%d", len(brokers))
    return brokers

def list_accounts() -> List[Account]:
    # Your schema: table is broker_accounts, NOT "accounts"; there is NO account.enabled column.
    # We only include accounts whose broker is enabled.
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT a.account_id, a.broker_id, a.label, a.account_code, a.margin
            FROM broker_accounts a
            JOIN brokers b ON b.broker_id = a.broker_id
            WHERE b.enabled = TRUE
            ORDER BY a.account_id
        """)
        rows = cur.fetchall()
    accounts = [Account(*r) for r in rows]
    log.debug("db.list_accounts count=%d", len(accounts))
    return accounts

def _fernet() -> Optional[Fernet]:
    key = get_fernet_key()
    if not key:
        return None
    try:
        return Fernet(key.encode("utf-8"))
    except Exception as e:
        log.warning("fernet.key.invalid %s", e)
        return None

def _decrypt(value_cipher: bytes) -> Optional[str]:
    f = _fernet()
    if not f:
        return None
    try:
        # value_cipher is bytea (already raw bytes). Fernet expects the base64 token bytes.
        # If the DB holds the exact Fernet token bytes, pass-through works:
        plain = f.decrypt(value_cipher)
        return plain.decode("utf-8")
    except InvalidToken:
        log.error("fernet.decrypt.invalid_token")
        return None
    except Exception as e:
        log.error("fernet.decrypt.error %s", e)
        return None

def get_account_creds(account_id: str) -> Dict[str, Optional[str]]:
    """
    Returns dict with possible keys: api_key, api_secret, base_url.
    If REFLEX_FERNET_KEY missing or decryption fails, values will be None (and we’ll log).
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT key, value_cipher
            FROM credentials
            WHERE scope_type = 'account' AND scope_id = %s
        """, (account_id,))
        rows = cur.fetchall()

    out: Dict[str, Optional[str]] = {"api_key": None, "api_secret": None, "base_url": None}
    for k, vc in rows:
        out[k] = _decrypt(vc)

    log.debug("db.get_account_creds account_id=%r has_api_key=%s has_api_secret=%s has_base_url=%s",
              account_id, bool(out["api_key"]), bool(out["api_secret"]), bool(out["base_url"]))
    return out
