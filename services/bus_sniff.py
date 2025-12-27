# scripts/bus_sniff.py
import os, json, redis, time

URL = os.getenv("GARNET_URL", "redis://127.0.0.1:6379")
r = redis.Redis.from_url(URL, decode_responses=True)
p = r.pubsub(ignore_subscribe_messages=True)
p.psubscribe("md.*")

print("Sniffing md.* (Ctrl+C to stop)")
try:
    n=0
    for m in p.listen():
        ch = m.get("channel")
        data = m.get("data")
        n+=1
        if n % 100 == 1:
            print(f"{time.strftime('%H:%M:%S')} {ch}")
            try:
                j = json.loads(data)
                if isinstance(j, dict):
                    print(" keys:", list(j.keys())[:8])
                elif isinstance(j, list) and j and isinstance(j[0], dict):
                    print(" keys(list[0]):", list(j[0].keys())[:8])
            except Exception:
                pass
except KeyboardInterrupt:
    pass
