# reflexAPI/worker.py
from __future__ import annotations
import os
import time
from common.utils import load_env
from common import logging as log
from common.metrics_http import start_metrics_http

SERVICE   = os.getenv("REFLEX__SERVICE", "reflex.api.worker")
COMPONENT = SERVICE
METRICS_PORT = int(os.getenv("REFLEX__METRICS_PORT", "7004"))

def main():
    try:
        load_env()
        start_metrics_http(SERVICE, METRICS_PORT)
        log.info(COMPONENT, "headless_worker_started", service=SERVICE, port=METRICS_PORT)
        try:
            while True:
                time.sleep(5)
        except KeyboardInterrupt:
            log.info(COMPONENT, "headless_worker_stopping", service=SERVICE)
    except Exception as e:
        log.error(COMPONENT, "worker_error", error=str(e))
        raise

if __name__ == "__main__":
    main()
