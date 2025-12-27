# datahub/replay_main.py
from __future__ import annotations

import asyncio
import os

from datahub.core import run_core


def main() -> None:
    # Prefer explicit REPLAY, but .env can still tweak other knobs
    os.environ.setdefault("REFLEX__HUB_MODE", "REPLAY")
    asyncio.run(run_core("REPLAY"))


if __name__ == "__main__":
    main()
