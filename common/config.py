from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import os
from dotenv import load_dotenv

load_dotenv()  # read .env in CWD (batch files set it)

def _env_bool(k: str, default=False) -> bool:
    v = os.getenv(k)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")

def _env_int(k: str, default: int) -> int:
    try:
        return int(os.getenv(k, str(default)))
    except Exception:
        return default

@dataclass(frozen=True)
class ServerCfg:
    host: str
    port: int

@dataclass(frozen=True)
class StorageCfg:
    pg_dsn: str
    parquet_root: Path

@dataclass(frozen=True)
class HubCfg:
    mode: str           # LIVE | REPLAY
    partitions: int     # # of live hub partitions
    index: int          # which partition this hub owns

@dataclass(frozen=True)
class LiveCfg:
    polygon_ws_key: str
    polygon_rest_key: str
    drop_copy_ticks: bool
    drop_copy_quotes: bool

@dataclass(frozen=True)
class ReplayCfg:
    session: str
    speed: float
    clock: str
    start: str | None
    end: str | None

@dataclass(frozen=True)
class AppCfg:
    instance_id: str
    server: ServerCfg
    storage: StorageCfg
    hub: HubCfg
    live: LiveCfg
    replay: ReplayCfg
    log_level: str
    metrics_enable: bool

def load_cfg() -> AppCfg:
    instance_id = os.getenv("REFLEX__INSTANCE_ID", "default")
    server = ServerCfg(
        host=os.getenv("REFLEX__SERVER__HOST", "0.0.0.0"),
        port=_env_int("REFLEX__SERVER__PORT", 7000),
    )
    storage = StorageCfg(
        pg_dsn=os.getenv("REFLEX__STORAGE__PG_DSN", "postgresql://reflex:***@localhost:5432/reflex"),
        parquet_root=Path(os.getenv("REFLEX__STORAGE__PARQUET_ROOT", "D:/reflex_parquet")),
    )
    hub = HubCfg(
        mode=os.getenv("REFLEX__HUB_MODE", "LIVE").upper(),
        partitions=_env_int("REFLEX__HUB_PARTITIONS", 1),
        index=_env_int("REFLEX__HUB_INDEX", 0),
    )
    live = LiveCfg(
        polygon_ws_key=os.getenv("POLYGON_KEY", ""),
        polygon_rest_key=os.getenv("POLYGON_KEY", ""),
        drop_copy_ticks=_env_bool("DROP_COPY_TICKS", True),
        drop_copy_quotes=_env_bool("DROP_COPY_QUOTES", False),
    )
    replay = ReplayCfg(
        session=os.getenv("REPLAY_SESSION", "2021-01-27"),
        speed=float(os.getenv("REPLAY_SPEED", "1.0")),
        clock=os.getenv("REPLAY_CLOCK", "wall"),
        start=os.getenv("REPLAY_START"),
        end=os.getenv("REPLAY_END"),
    )
    return AppCfg(
        instance_id=instance_id,
        server=server,
        storage=storage,
        hub=hub,
        live=live,
        replay=replay,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        metrics_enable=_env_bool("METRICS_ENABLE", True),
    )
