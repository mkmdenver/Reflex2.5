# tools/tap_bars.py
#
# Bar tap: subscribe to DataHub 1m bars (MessagePack) and print readable output.
#
# Uses canonical env vars:
#   REFLEX_MODE=LIVE|REPLAY   (default LIVE)
#   REFLEX_DATAHUB_BARS1M_PUB_LIVE=hub.bars1m.pub.live
#   REFLEX_DATAHUB_BARS1M_PUB_REPLAY=hub.bars1m.pub.replay
#
# Optional overrides:
#   --channel <redis-channel>
#   --raw         (print decoded dict as one-line json-ish)
#   --limit N     (exit after N bars)
#
# KISS rules:
# - auto-load root .env and .env.local
# - add repo root to sys.path so common.* imports work

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

# --- repo root bootstrap + dotenv load (must happen before importing common.*) ---
_THIS = Path(__file__).resolve()
_repo_root: Optional[Path] = None
for p in [_THIS.parent, *_THIS.parents]:
    if (p / ".env").exists():
        _repo_root = p
        break
if _repo_root is None:
    _repo_root = _THIS.parents[1]

if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))


def _load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


_load_dotenv_file(_repo_root / ".env")
_load_dotenv_file(_repo_root / ".env.local")

from common.bus import subscribe, unpack  # MessagePack decode + Redis subscribe


def _f(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None


def _i(x: Any) -> Optional[int]:
    if x is None:
        return None
    try:
        return int(x)
    except Exception:
        return None


def _pick_channel(mode: str) -> str:
    mode = (mode or "LIVE").strip().upper()
    if mode == "REPLAY":
        ch = os.getenv("REFLEX_DATAHUB_BARS1M_PUB_REPLAY", "").strip()
        if not ch:
            raise SystemExit("[ERROR] Missing REFLEX_DATAHUB_BARS1M_PUB_REPLAY in environment.")
        return ch
    else:
        ch = os.getenv("REFLEX_DATAHUB_BARS1M_PUB_LIVE", "").strip()
        if not ch:
            raise SystemExit("[ERROR] Missing REFLEX_DATAHUB_BARS1M_PUB_LIVE in environment.")
        return ch


def _fmt_ts(t: Any) -> str:
    if t is None:
        return "-"
    if isinstance(t, str):
        return t
    n = _f(t)
    if n is None:
        return str(t)
    if n > 10_000_000_000:  # ms epoch
        n = n / 1000.0
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(n))
    except Exception:
        return str(t)


def _extract_bar(d: Dict[str, Any]) -> Dict[str, Any]:
    sym = d.get("sym") or d.get("symbol") or d.get("S")
    o = d.get("o") or d.get("open")
    h = d.get("h") or d.get("high")
    l = d.get("l") or d.get("low")
    c = d.get("c") or d.get("close")
    v = d.get("v") or d.get("volume")
    vw = d.get("vw") or d.get("vwap")
    n = d.get("n") or d.get("trades")
    t = d.get("t") or d.get("ts") or d.get("bar_ts") or d.get("timestamp") or d.get("minute_key")


    return {
        "sym": str(sym).upper() if sym else None,
        "t": t,
        "o": _f(o),
        "h": _f(h),
        "l": _f(l),
        "c": _f(c),
        "v": _i(v),
        "vw": _f(vw),
        "n": _i(n),
    }


def _one_line(bar: Dict[str, Any]) -> str:
    ts = _fmt_ts(bar.get("t"))
    sym = bar.get("sym") or "-"
    o = bar.get("o")
    h = bar.get("h")
    l = bar.get("l")
    c = bar.get("c")
    v = bar.get("v")
    n = bar.get("n")
    vw = bar.get("vw")

    # guard formatting if any floats missing
    def ff(x: Optional[float]) -> str:
        return f"{x:.4f}" if isinstance(x, (int, float)) else "-"

    return (
        f"{ts}  {sym:6}  "
        f"O={ff(o)} H={ff(h)} L={ff(l)} C={ff(c)}  "
        f"V={v if v is not None else '-'} N={n if n is not None else '-'} VW={vw if vw is not None else '-'}"
    )


async def main() -> None:
    ap = argparse.ArgumentParser(description="Tap DataHub 1m bars and print readable lines.")
    ap.add_argument("--channel", default="", help="Override Redis channel (otherwise uses canonical env vars).")
    ap.add_argument("--raw", action="store_true", help="Print decoded dict instead of formatted one-line.")
    ap.add_argument("--limit", type=int, default=0, help="Exit after N bars (0 = run forever).")
    args = ap.parse_args()

    mode = os.getenv("REFLEX_MODE", "LIVE")
    channel = args.channel.strip() if args.channel else _pick_channel(mode)

    print(f"[tap_bars] ROOT={_repo_root}")
    print(f"[tap_bars] REFLEX_MODE={mode}")
    print(f"[tap_bars] channel={channel}")
    print("[tap_bars] waiting... (Ctrl+C to stop)")

    ps = await subscribe(channel)
    count = 0

    async for msg in ps.listen():  # type: ignore[attr-defined]
        if msg.get("type") != "message":
            continue

        obj = unpack(msg["data"])
        if not isinstance(obj, dict):
            if args.raw:
                print(obj)
            continue

        bar = _extract_bar(obj)
        if bar["sym"] is None or bar["o"] is None or bar["c"] is None:
            if args.raw:
                print(obj)
            continue

        count += 1
        if args.raw:
            print(obj)
        else:
            print(_one_line(bar))

        if args.limit and count >= args.limit:
            print(f"[tap_bars] limit reached: {args.limit}")
            return


if __name__ == "__main__":
    asyncio.run(main())
