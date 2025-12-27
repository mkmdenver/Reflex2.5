# trader/envload.py
from __future__ import annotations
import os
from typing import Dict
from common.utils import load_env as _load_env_common

def _normalize_powershell_vars(env: Dict[str, str]) -> None:
    """
    If someone accidentally pasted PowerShell-style lines like `$env:FOO=bar`
    into a .env, ensure we also expose FOO=bar (without clobbering).
    """
    for k in list(env.keys()):
        if k.startswith("$env:"):
            norm = k[len("$env:"):]
            if norm and norm not in os.environ:
                os.environ[norm] = env[k]
                env[norm] = env[k]

def load_dotenv_if_needed() -> Dict[str, str]:
    """
    Thin, project-consistent loader:
      - Loads .env via common.utils.load_env
      - Then overlays .env.local if present (same loader)
      - Normalizes any `$env:NAME` keys to `NAME`
      - Never clobbers existing process env
    Returns the merged env dict for convenience.
    """
    env = _load_env_common(".env")
    # Overlay .env.local (if present)
    _ = _load_env_common(".env.local")
    env.update({k: v for k, v in os.environ.items()})  # reflect any new keys set by second pass
    _normalize_powershell_vars(env)
    return env
