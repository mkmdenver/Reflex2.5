# trader/adapters/base.py
from __future__ import annotations
from abc import ABC, abstractmethod
from ..portfolio_core import AccountSnapshot

class BrokerAdapter(ABC):
    @abstractmethod
    async def snapshot(self) -> AccountSnapshot: ...
