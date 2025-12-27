
"""
Probe tool to sanity-check adapters and (optionally) place tiny test trades.

SAFE BY DEFAULT: will NOT place any orders unless BOTH are true:
- Environment variable TEST_TRADES_ENABLE=true
- --account <account_id> AND --symbol <symbol> AND --qty <qty> are provided

Usage:
  python -m tools.probe_brokers list
  python -m tools.probe_brokers ping
  TEST_TRADES_ENABLE=true python -m tools.probe_brokers micro --account alpaca:paper:acct1 --symbol SPY --qty 1 --side buy --order-type market
"""
import os, argparse, json
from trader.core import AdapterManager

def cmd_list(args):
    am = AdapterManager()
    catalog = am.accounts_catalog()
    print(json.dumps(catalog, indent=2))

def cmd_ping(args):
    am = AdapterManager()
    for acc_id, meta in am.accounts_catalog().items():
        try:
            snap = am.account_snapshot(acc_id)
            print(f"[OK] {acc_id}: positions={len(snap.get('positions', []))} open_orders={len(snap.get('open_orders', []))}")
        except Exception as e:
            print(f"[FAIL] {acc_id}: {e}")

def cmd_micro(args):
    if os.getenv("TEST_TRADES_ENABLE","").lower() != "true":
        print("Refusing to place test orders: set TEST_TRADES_ENABLE=true to proceed.")
        return
    if not (args.account and args.symbol and args.qty and args.side and args.order_type):
        print("Missing required args for micro test.")
        return
    am = AdapterManager()
    order = {
        "coid": f"probe-{args.account}-{args.symbol}",
        "symbol": args.symbol,
        "side": args.side,
        "order_type": args.order_type,
        "qty": int(args.qty),
        "time_in_force": "day"
    }
    ack = am.submit(args.account, order)
    print(json.dumps({"account": args.account, "ack": ack}, indent=2))

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("ping")
    sp = sub.add_parser("micro")
    sp.add_argument("--account")
    sp.add_argument("--symbol")
    sp.add_argument("--qty", type=int)
    sp.add_argument("--side", choices=["buy","sell"])
    sp.add_argument("--order-type", choices=["market","limit"], dest="order_type")
    args = ap.parse_args()
    if args.cmd == "list": cmd_list(args)
    elif args.cmd == "ping": cmd_ping(args)
    elif args.cmd == "micro": cmd_micro(args)
