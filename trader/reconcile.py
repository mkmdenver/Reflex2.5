# trader/reconcile.py
"""
Legacy Reconciler used by some cockpit routes.
Modern reconcile is owned by PortfolioManager; this remains as a compatibility shim.
"""
from .core import Store, Alerts, AdapterManager

class Reconciler:
    def __init__(self, store: Store, alerts: Alerts, adapters: AdapterManager):
        self.store = store
        self.alerts = alerts
        self.adapters = adapters

    def run_once(self) -> bool:
        try:
            catalog = self.adapters.accounts_catalog()
            positions = {}
            open_orders = {}
            for acc_id in catalog:
                snap = self.adapters.account_snapshot(acc_id)
                positions[acc_id] = snap.get("positions", [])
                open_orders[acc_id] = snap.get("open_orders", [])
            self.store.write_accounts(catalog)
            self.store.write_positions(positions)
            self.store.write_open_orders(open_orders)
            self.store.set_reconciled(True)
            self.alerts.emit("RECONCILE_OK")
            return True
        except Exception as e:
            self.store.set_reconciled(False)
            self.alerts.emit("RECONCILE_FAIL", error=str(e))
            return False
