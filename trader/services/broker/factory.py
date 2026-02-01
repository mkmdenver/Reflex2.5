# trader/broker/factory.py
from __future__ import annotations
from typing import Optional

from common import logging as log

from .sim import BrokerSim
from .alpaca import BrokerAlpaca, build_from_db as build_alpaca_from_db

COMPONENT = "trader.broker.factory"


def make_broker(broker_name: str, account_id: Optional[str] = None):
    """
    Unified builder used by BrokerWorker.
      - sim: returns BrokerSim (no creds)
      - alpaca: wraps the *HTTP adapter* through BrokerAlpaca so all orders land
                in the same centralized HTTP implementation (supports extended_hours).
    """
    b = (broker_name or "").lower()

    if b == "sim":
        acct = account_id or "sim:paper"
        log.info(COMPONENT, "make.sim", account_id=acct)
        return BrokerSim(acct)

    if b == "alpaca":
        acct = account_id or "alpaca:paper"
        # Always build via DB creds → BrokerAlpaca → AlpacaHttpAdapter
        br = build_alpaca_from_db(acct)
        log.info(COMPONENT, "make.alpaca", account_id=acct)
        return br

    raise ValueError(f"Unknown broker '{broker_name}'")
