import os
import asyncio
import redis.asyncio as aioredis

# Minimal tap for eval.simple_filter_stream (or any channel)
# Usage:
#   set CHAN=eval.simple_filter_stream
#   python simple_filter_tap.py
#
# Or:
#   python simple_filter_tap.py eval.fts_rbf.stream

def get_channel(argv) -> str:
    if len(argv) >= 2 and argv[1].strip():
        return argv[1].strip()
    return os.getenv("CHAN", "eval.simple_filter_stream")

def get_redis_url() -> str:
    return (
        os.getenv("GARNET_URL")
        or os.getenv("REDIS_URL")
        or os.getenv("REFLEX_REDIS_URL")
        or "redis://127.0.0.1:6379/0"
    )

async def main():
    import sys
    chan = get_channel(sys.argv)
    url = get_redis_url()

    r = aioredis.from_url(url, decode_responses=True)
    pubsub = r.pubsub()
    await pubsub.subscribe(chan)

    print(f"[tap] redis_url: {url}")
    print(f"[tap] subscribed: {chan}")

    n = 0
    try:
        async for msg in pubsub.listen():
            if msg is None:
                continue
            if msg.get("type") != "message":
                continue
            n += 1
            data = msg.get("data")
            # Don’t assume JSON; just show size + first slice
            preview = data if isinstance(data, str) else repr(data)
            if isinstance(preview, str) and len(preview) > 240:
                preview = preview[:240] + "..."
            print(f"[tap] msg {n} len={len(str(data)) if data is not None else 0}  {preview}")
    except KeyboardInterrupt:
        print("\n[tap] stopped")
    finally:
        try:
            await pubsub.unsubscribe(chan)
        except Exception:
            pass
        try:
            await pubsub.aclose()
        except Exception:
            pass
        try:
            await r.aclose()
        except Exception:
            pass

if __name__ == "__main__":
    asyncio.run(main())
