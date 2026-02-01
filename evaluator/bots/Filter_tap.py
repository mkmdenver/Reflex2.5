import os
import sys
import json
import asyncio
from typing import Any, Optional

import redis.asyncio as aioredis


def _get_redis_url() -> str:
    return (
        os.getenv("GARNET_URL")
        or os.getenv("REDIS_URL")
        or os.getenv("REFLEX_REDIS_URL")
        or "redis://127.0.0.1:6379/0"
    )


def _get_channel(argv: list[str]) -> str:
    if len(argv) >= 2 and argv[1].strip():
        return argv[1].strip()
    return os.getenv("CHAN", "eval.rbf_filter_stream")


def _get_active_key() -> str:
    return os.getenv("ACTIVE_KEY", "eval:fts_rbf:active")


def _get_durable_key() -> str:
    return os.getenv("DURABLE_KEY", "eval:fts_rbf:durable")


def _get_durable_tail_n() -> int:
    try:
        return int(os.getenv("DURABLE_TAIL_N", "50"))
    except Exception:
        return 50


def _b2s(x: Any) -> str:
    if isinstance(x, (bytes, bytearray)):
        try:
            return x.decode("utf-8", errors="replace")
        except Exception:
            return repr(x)
    return str(x)


def _pretty_obj(obj: Any, max_len: int = 600) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        s = repr(obj)
    if len(s) > max_len:
        s = s[:max_len] + "..."
    return s


def _try_parse_durable_entry(entry: Any) -> Any:
    # Durable list entries are typically JSON strings.
    s = _b2s(entry).strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        # If it isn't JSON, just return raw string/bytes preview
        return s


async def main() -> None:
    url = _get_redis_url()
    chan = _get_channel(sys.argv)
    active_key = _get_active_key()
    durable_key = _get_durable_key()
    tail_n = _get_durable_tail_n()

    # IMPORTANT: PubSub payloads may be binary (msgpack). Do NOT decode as UTF-8 automatically.
    r = aioredis.from_url(url, decode_responses=False)

    print(f"[tap] redis_url: {url}")
    print(f"[tap] channel: {chan}")
    print(f"[tap] active_key: {active_key}")
    print(f"[tap] durable_key: {durable_key} (tail_n={tail_n})")

    # Try to use project unpack if available; otherwise show raw bytes.
    try:
        from common.bus import unpack as _unpack  # type: ignore
    except Exception:
        _unpack = None

    # ---- Catch-up 1: snapshot of current active set ----
    try:
        raw_active = await r.smembers(active_key)
        active = sorted({_b2s(x) for x in raw_active if x})
        print(f"[tap] snapshot active_count={len(active)}")
        if active:
            # Print a compact single-line list; keep it readable
            print("[tap] snapshot active:", ", ".join(active[:200]) + ("" if len(active) <= 200 else " ..."))
    except Exception as exc:
        print(f"[tap] snapshot ERROR: {exc!r}")

    # ---- Catch-up 2 (optional): last N durable events ----
    if tail_n > 0:
        try:
            llen = await r.llen(durable_key)
            start = max(0, llen - tail_n)
            raw = await r.lrange(durable_key, start, -1)
            print(f"[tap] durable llen={llen} showing_last={len(raw)}")
            for i, ent in enumerate(raw, 1):
                obj = _try_parse_durable_entry(ent)
                if obj is None:
                    continue
                print(f"[tap] durable {i}/{len(raw)} {_pretty_obj(obj)}")
        except Exception as exc:
            print(f"[tap] durable ERROR: {exc!r}")

    # ---- Live subscribe for deltas ----
    pubsub = r.pubsub()
    await pubsub.subscribe(chan)
    print(f"[tap] subscribed: {chan}")

    n = 0
    try:
        async for msg in pubsub.listen():
            if msg is None or msg.get("type") != "message":
                continue
            n += 1
            data = msg.get("data")

            if isinstance(data, (bytes, bytearray)):
                if _unpack is not None:
                    try:
                        obj = _unpack(data)
                        print(f"[tap] live {n} {_pretty_obj(obj)}")
                        continue
                    except Exception:
                        pass
                # fallback: raw bytes preview
                preview = repr(bytes(data[:120]))
                print(f"[tap] live {n} <bytes {len(data)} {preview}>")
            else:
                # rare: already decoded / non-bytes payload
                print(f"[tap] live {n} {_pretty_obj(data)}")

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
