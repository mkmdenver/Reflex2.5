# trader/portfolio_manager.py
# v1.3 — cred dict/object compatibility; better logging; updated_at stamps.

from __future__ import annotations
import os
import logging
from typing import Dict, Any, Optional
from datetime import datetime, timezone

from .adapters.types import AccountSnapshot, Position
from .adapters.alpaca_adapter import AlpacaAdapter
from . import db_brokers

log = logging.getLogger("trader.portfolio_manager")


def _as_dict(obj: Any) -> Dict[str, Any]:
    """Return dict for either dict or simple attribute container."""
    if isinstance(obj, dict):
        return obj
    # fall back to attribute access if available
    out: Dict[str, Any] = {}
    for k in ("api_key", "api_secret", "base_url"):
        out[k] = getattr(obj, k, None)
    return out


class SimAdapter:
    broker_id = "sim"
    kind = "sim"

    def __init__(self, account_id: str, starting_cash: float = 100_000.0, margin: bool = True):
        self.account_id = account_id
        self._cash = float(starting_cash)
        self._margin = margin
        self._positions: Dict[str, Position] = {}

        log.info(
            "adapter.init account_id=%r broker_id=%r kind=%r base=%r key_present=%s secret_present=%s",
            self.account_id, self.broker_id, self.kind, None, False, False
        )

    def refresh_snapshot(self) -> AccountSnapshot:
        bp = self._cash * (2.0 if self._margin else 1.0)
        return AccountSnapshot(
            account_id=self.account_id,
            cash=self._cash,
            equity=self._cash,
            buying_power=bp,
            positions=list(self._positions.values()),
        )

    def close(self) -> None:
        return


class PortfolioManager:
    """Holds broker adapters keyed by account_id and produces normalized snapshots."""

    def __init__(self, instance: Optional[str] = None):
        self.instance: str = instance or os.getenv("INSTANCE") or "liveA"
        self.adapters: Dict[str, Any] = {}
        self.portfolio: Dict[str, AccountSnapshot] = {}
        self._lock = __import__("asyncio").Lock()
        self._last_reconcile_at: Optional[datetime] = None
        self._events: Dict[str, list] = {}
        log.info("init instance=%r", self.instance)

    async def register_from_db(self) -> None:
        log.info("register_from_db.begin instance=%r", self.instance)
        accounts = list(db_brokers.list_accounts())
        log.debug("db.list_accounts count=%d", len(accounts))

        created = 0
        for a in accounts:
            account_id = getattr(a, "account_id", None) or (a.get("account_id") if isinstance(a, dict) else None)
            broker_id = getattr(a, "broker_id", None) or (a.get("broker_id") if isinstance(a, dict) else None)
            if not account_id or not broker_id:
                log.warning("account row missing ids: %r", a)
                continue

            raw = db_brokers.get_account_creds(account_id)
            creds = _as_dict(raw)
            base = (creds.get("base_url") or "").strip()
            key_id = (creds.get("api_key") or "").strip()
            secret = (creds.get("api_secret") or "").strip()

            log.debug(
                "db.get_account_creds account_id=%r has_api_key=%s has_api_secret=%s has_base_url=%s",
                account_id, bool(key_id), bool(secret), bool(base)
            )

            if broker_id == "alpaca":
                adapter = AlpacaAdapter(account_id=account_id, base=base, key_id=key_id, secret=secret)
            elif broker_id == "sim":
                margin = account_id.endswith(":margin")
                adapter = SimAdapter(account_id=account_id, starting_cash=100_000.0, margin=margin)
            else:
                log.warning("unknown broker_id=%r for account_id=%r — skipping", broker_id, account_id)
                continue

            self.adapters[account_id] = adapter
            created += 1
            log.info(
                "adapter.init account_id=%r broker_id=%r kind=%r base=%r key_present=%s secret_present=%s",
                getattr(adapter, "account_id", account_id),
                getattr(adapter, "broker_id", "?"),
                getattr(adapter, "kind", "?"),
                getattr(adapter, "base", None),
                bool(getattr(adapter, "key_id", "")),
                bool(getattr(adapter, "secret", "")),
            )

        log.info("register_from_db.done adapters=%d created=%d", len(self.adapters), created)

    def _calc_totals(self, snaps: Dict[str, AccountSnapshot]) -> Dict[str, float]:
        tot_cash = sum(float(s.cash or 0) for s in snaps.values())
        tot_equity = sum(float(s.equity or 0) for s in snaps.values())
        tot_bp = sum(float(s.buying_power or 0) for s in snaps.values())
        return {"cash": tot_cash, "equity": tot_equity, "buying_power": tot_bp}

    async def reconcile(self) -> Dict[str, AccountSnapshot]:
        now_iso = datetime.now(timezone.utc).isoformat()
        snaps: Dict[str, AccountSnapshot] = {}
        for aid, adapter in self.adapters.items():
            try:
                snap = adapter.refresh_snapshot()
                snap.updated_at = now_iso
            except Exception:
                log.exception("reconcile.error account_id=%r", aid)
                prev = self.portfolio.get(aid)
                if prev:
                    prev.updated_at = now_iso
                    snap = prev
                else:
                    snap = AccountSnapshot(aid, 0.0, 0.0, 0.0, [], updated_at=now_iso)
            snaps[aid] = snap

        self.portfolio = snaps
        return snaps


    async def reconcile_once(self, account_id: Optional[str] = None) -> None:
        """Refresh broker snapshots.

        - If account_id is provided, refresh just that account.
        - Otherwise refresh all accounts.

        This keeps Trader's app.py free to call `reconcile_once(account_id=...)`
        without exploding when the PortfolioManager evolves.
        """
        async with self._lock:
            if account_id:
                adapter = self.adapters.get(account_id)
                if adapter is None:
                    return
                now_iso = datetime.now(timezone.utc).isoformat()
                try:
                    snap = adapter.refresh_snapshot()
                    snap.updated_at = now_iso
                    self.portfolio[account_id] = snap
                except Exception:
                    log.exception("reconcile_once.account.error account_id=%r", account_id)
                self._last_reconcile_at = datetime.now(timezone.utc)
                return

            await self.reconcile()
            self._last_reconcile_at = datetime.now(timezone.utc)

    async def reconcile_account(self, account_id: str) -> None:
        """Refresh a single account snapshot immediately (used by broker events)."""
        async with self._lock:
            adapter = self.adapters.get(account_id)
            if adapter is None:
                return
            now_iso = datetime.now(timezone.utc).isoformat()
            try:
                snap = adapter.refresh_snapshot()
                snap.updated_at = now_iso
                self.portfolio[account_id] = snap
            except Exception:
                log.exception("reconcile_account.error account_id=%r", account_id)

    def record_broker_event(self, account_id: str, event: Dict[str, Any]) -> None:
        """Record a broker event for debugging/UI and mark account dirty."""
        try:
            dq = self._events.setdefault(account_id, [])
            dq.append({"ts": datetime.now(timezone.utc).isoformat(), "event": event})
            if len(dq) > 200:
                del dq[:-200]
        except Exception:
            pass

    def get_snapshots(self) -> Dict[str, Dict[str, Any]]:
        return {aid: s.to_dict() for aid, s in self.portfolio.items()}

    def get_state(self) -> Dict[str, Any]:
        totals = self._calc_totals(self.portfolio)
        return {
            "instance": self.instance,
            "accounts": {aid: s.to_dict() for aid, s in self.portfolio.items()},
            "totals": totals,
            "risk": {
                "portfolio_stop_tripped": False,
                "portfolio_stop_reason": "",
                "constraints": {},
            },
        }

    def close(self) -> None:
        for a in self.adapters.values():
            try:
                a.close()
            except Exception:
                pass
