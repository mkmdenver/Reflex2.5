# datahub/adapters/__init__.py
from __future__ import annotations
import os
import inspect

def _construct(cls, **kwargs):
    """Call cls with only the kwargs its __init__ actually accepts."""
    sig = inspect.signature(cls.__init__)
    params = set(sig.parameters.keys())
    params.discard("self")
    # allow simple aliases for the API key
    if "api_key" not in params:
        for alias in ("key", "token", "apikey", "apiKey"):
            if alias in params and "api_key" in kwargs:
                kwargs[alias] = kwargs["api_key"]
                break
    filtered = {k: v for k, v in kwargs.items() if k in params}
    return cls(**filtered)

def make_adapter(mode: str):
    mode = (mode or os.getenv("REFLEX__HUB_MODE", "LIVE")).upper()
    if mode == "REPLAY":
        from .replay_adapter import ReplayAdapter
        return ReplayAdapter()

    # LIVE
    from .live_adapter import LIVEAdapter
    api_key = os.getenv("POLYGON_API_KEY") or os.getenv("POLYGON_KEY")
    ws_url  = os.getenv("POLYGON_WS_URL", "wss://socket.polygon.io/stocks")
    # Only pass args that LIVEAdapter actually supports
    return _construct(LIVEAdapter, api_key=api_key, ws_url=ws_url)
