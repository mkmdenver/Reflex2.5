# common/utils.py
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional, Tuple

def _split_kv(line: str) -> Optional[Tuple[str, str]]:
    """
    Parse a dotenv-style line. Supports:
      - KEY=VAL
      - export KEY=VAL
      - quoted values: KEY="a b # c", KEY='a b'
      - inline comments when unquoted: KEY=abc  # comment
      - empty values: KEY=
    Returns (key, value) or None if the line is ignorable.
    """
    s = line.strip()
    if not s or s.startswith("#"):
        return None

    # Allow 'export KEY=VAL'
    if s.lower().startswith("export "):
        s = s[7:].lstrip()

    # Find first '='
    i = s.find("=")
    if i <= 0:
        return None
    k = s[:i].strip()
    v = s[i + 1 :].strip()

    # If quoted, keep everything inside the quotes verbatim (minus the quotes)
    if (len(v) >= 2) and ((v[0] == v[-1]) and v[0] in ("'", '"')):
        q = v[0]
        v = v[1:-1]
    else:
        # Unquoted: strip trailing inline comment starting with ' #'
        # (do not strip '#' that are part of URLs like http://…#frag if no leading space)
        hash_at = v.find(" #")
        if hash_at != -1:
            v = v[:hash_at].rstrip()

    # Unescape common sequences in quoted values (kept simple)
    v = v.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")
    return (k, v)

def _merge_env_file(env: Dict[str, str], env_path: Path) -> None:
    """
    Merge keys from a .env-like file into both os.environ and the returned env dict,
    but NEVER override already-set process env vars.
    """
    try:
        if not env_path.exists() or not env_path.is_file():
            return
        # Read with utf-8-sig to drop BOM if present
        text = env_path.read_text(encoding="utf-8-sig")
    except Exception:
        return

    for raw in text.splitlines():
        parsed = _split_kv(raw)
        if not parsed:
            continue
        k, v = parsed
        if k not in os.environ:
            os.environ[k] = v
            env[k] = v  # reflect the addition in our returned snapshot

def load_env(env_filename: str = ".env") -> Dict[str, str]:
    """
    Load environment variables from (in order):
      1) CWD / .env
      2) CWD / .env.local
      3) repo root (two levels up from this file) / .env
      4) repo root / .env.local
      5) package parent (common/..) / .env
      6) package parent / .env.local

    Existing process env always wins.
    Returns a snapshot dict of the final environment (same keys as os.environ).
    """
    env: Dict[str, str] = dict(os.environ)

    here = Path(__file__).resolve()
    parents = list(here.parents)

    # Safely compute “repo root” (~two up from common/utils.py if present)
    repo_root = parents[2] if len(parents) > 2 else parents[-1]
    pkg_parent = here.parent.parent  # common/..

    candidates = [
        Path.cwd() / env_filename,
        Path.cwd() / f"{env_filename}.local",
        repo_root / env_filename,
        repo_root / f"{env_filename}.local",
        pkg_parent / env_filename,
        pkg_parent / f"{env_filename}.local",
    ]

    seen: set[Path] = set()
    for p in candidates:
        try:
            p = p.resolve()
        except Exception:
            continue
        if p not in seen:
            _merge_env_file(env, p)
            seen.add(p)

    return env

# Optional helpers (lightweight, no surprises)
def env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")

def env_int(key: str, default: int = 0) -> int:
    try:
        return int(os.getenv(key, "").strip())
    except Exception:
        return default
