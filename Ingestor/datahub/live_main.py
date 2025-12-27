# datahub/live_main.py
from __future__ import annotations

import asyncio
import os

from datahub.core import run_core


def main() -> None:
    # Prefer explicit LIVE, but let .env override if someone insists.
    os.environ.setdefault("REFLEX__HUB_MODE", "LIVE")
    asyncio.run(run_core("LIVE"))


if __name__ == "__main__":
    main()
