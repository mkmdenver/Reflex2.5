# trader/core.py
from __future__ import annotations
import os, sys, logging
from typing import Optional

_LOGGER: Optional[logging.Logger] = None

def setup_logging(level: Optional[str] = None) -> None:
    """Initialize root logger once. Accepts a level string or uses LOG_LEVEL env."""
    global _LOGGER
    if _LOGGER:
        return
    lvl = (level or os.getenv("LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, lvl, logging.INFO),
        format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    )
    _LOGGER = logging.getLogger("trader")
    _LOGGER.debug("logging.initialized level=%s numeric=%s", lvl, getattr(logging, lvl, logging.INFO))

def get_logger(name: str = "trader") -> logging.Logger:
    global _LOGGER
    if _LOGGER is None:
        setup_logging()
    return logging.getLogger(name)

log = get_logger("trader")

def kvlog(msg: str, **kv):
    get_logger("trader").info("%s %s", msg, " ".join(f"{k}={repr(v)}" for k,v in kv.items()))

def get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(name, default)

def get_instance_id() -> str:
    return get_env("REFLEX_INSTANCE_ID", "live")

def get_redis_url() -> str:
    url = get_env("GARNET_URL")
    if not url:
        get_logger("trader").warning("env.redis_url.missing")
        return "redis://127.0.0.1:6379/0"
    return url

def get_broker_dsn() -> str:
    """Honor your canonical names; prefer BROKER_DATABASE_URL, fallback to REFLEX_BROKER_DSN."""
    dsn = get_env("BROKER_DATABASE_URL") or get_env("REFLEX_BROKER_DSN")
    if not dsn:
        raise RuntimeError("Broker DSN not configured (BROKER_DATABASE_URL or REFLEX_BROKER_DSN).")
    return dsn

def get_fernet_key() -> Optional[str]:
    """REFLEX_FERNET_KEY is optional; if missing we’ll return creds=None with a warning."""
    key = get_env("REFLEX_FERNET_KEY")
    if not key:
        get_logger("trader").debug("env.fernet_key.missing")
    return key

def get_trader_api_port() -> int:
    return int(get_env("TRADER_API_PORT", "7002"))
