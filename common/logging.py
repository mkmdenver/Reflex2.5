import json, sys, time, os

LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}

INSTANCE = os.getenv("REFLEX__INSTANCE_ID", "default")
MODE = os.getenv("REFLEX__HUB_MODE", "LIVE")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


def _enabled(level: str) -> bool:
    return LEVELS.get(level, 20) >= LEVELS.get(LOG_LEVEL, 20)


def log(level: str, component: str, msg: str, **extra):
    if not _enabled(level):
        return
    rec = {
        "ts": time.time(),
        "level": level,
        "instance": INSTANCE,
        "mode": MODE,
        "component": component,
        "msg": msg,
    }
    if extra:
        rec["extra"] = extra
    sys.stdout.write(json.dumps(rec) + "\n")
    sys.stdout.flush()


def debug(c, m, **k): log("DEBUG", c, m, **k)
def info(c, m, **k):  log("INFO",  c, m, **k)
def warn(c, m, **k):  log("WARN",  c, m, **k)
def error(c, m, **k): log("ERROR", c, m, **k)


# ----------------------------------------------------------------------
# Back-compat: get_logger("component") with .info/.error etc.
# ----------------------------------------------------------------------
def get_logger(component: str):
    """
    Tiny adapter so code can do:

        log = get_logger("datahub")
        log.info("boot instance=%s mode=%s", inst, mode)

    while still using the JSON logger above.
    """

    class _Logger:
        def __init__(self, comp: str):
            self._c = comp

        def debug(self, msg: str, *args, **kwargs):
            if args:
                msg = msg % args
            debug(self._c, msg, **kwargs)

        def info(self, msg: str, *args, **kwargs):
            if args:
                msg = msg % args
            info(self._c, msg, **kwargs)

        def warn(self, msg: str, *args, **kwargs):
            if args:
                msg = msg % args
            warn(self._c, msg, **kwargs)

        def error(self, msg: str, *args, **kwargs):
            if args:
                msg = msg % args
            error(self._c, msg, **kwargs)

    return _Logger(component)
