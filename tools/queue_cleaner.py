# tools/queue_cleaner.py
import argparse, json, os, sys, datetime
import redis

DEFAULT_KEYS = [
    "reflex:intents:auto",
    "reflex:intents.list",
    "reflex:events.orders",
]

def conn():
    url = os.getenv("GARNET_URL", "redis://127.0.0.1:6379")
    return redis.Redis.from_url(url, decode_responses=True)

def discover_keys(r):
    """Return list of (key, type) for reflex:* where type in {list, stream}"""
    found = []
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor=cursor, match="reflex:*", count=500)
        for k in keys:
            t = r.type(k)
            if isinstance(t, bytes):
                t = t.decode()
            if t in ("list", "stream"):
                found.append((k, t))
        if cursor == 0:
            break
    return found

def peek_list(r, key, n=3):
    length = r.llen(key)
    head = r.lrange(key, 0, min(n-1, max(0, length-1)))
    tail = r.lrange(key, max(0, length-n), length-1) if length else []
    return length, head, tail

def deadletter_key(orig):
    day = datetime.datetime.utcnow().strftime("%Y%m%d")
    return f"reflex:deadletter:{day}:{orig}"

def drain_list(r, key):
    dl = deadletter_key(key)
    r.delete(dl)  # fresh bucket for today
    moved = 0
    pipe = r.pipeline()
    while True:
        chunk = r.lrange(key, 0, 999)
        if not chunk:
            break
        r.ltrim(key, len(chunk), -1)
        for item in chunk:
            pipe.rpush(dl, item)  # preserve order
        pipe.execute()
        moved += len(chunk)
    return moved, dl

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["stats","drain","purge"], required=True)
    ap.add_argument("--keys", nargs="*", default=DEFAULT_KEYS,
                    help="Explicit keys. Ignored if --discover is set (except in stats we include streams too).")
    ap.add_argument("--discover", action="store_true",
                    help="Auto-discover reflex:* lists (and streams in stats mode).")
    args = ap.parse_args()

    r = conn()
    print(f"[env] GARNET_URL={os.getenv('GARNET_URL','redis://127.0.0.1:6379')}")

    keys = args.keys
    stream_keys = []
    if args.discover:
        dk = discover_keys(r)
        stream_keys = [k for k,t in dk if t == "stream"]
        list_keys   = [k for k,t in dk if t == "list"]
        keys = (list_keys + stream_keys) if args.mode == "stats" else list_keys

    print(f"[plan] mode={args.mode} keys={keys}")

    for k in keys:
        t = r.type(k)
        if isinstance(t, bytes):
            t = t.decode()

        if args.mode == "stats":
            if t == "list":
                length, head, tail = peek_list(r, k)
                print(f"\n[{k}] type=list len={length}")
                if head: print("  head:", head[:3])
                if tail and tail != head: print("  tail:", tail[:3])
            elif t == "stream":
                try:
                    xlen = r.xlen(k)
                except Exception:
                    xlen = "n/a"
                print(f"\n[{k}] type=stream len={xlen}")
            else:
                print(f"\n[{k}] type={t}")
        elif args.mode == "drain":
            if t != "list":
                print(f"[SKIP] {k}: type={t} (drain supports list only)")
                continue
            moved, dl = drain_list(r, k)
            print(f"[DRAIN] {k} -> {dl} moved={moved}")
        elif args.mode == "purge":
            r.delete(k)
            print(f"[PURGE] {k} deleted")

    print("[done]")

if __name__ == "__main__":
    main()
