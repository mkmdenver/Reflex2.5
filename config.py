# cockpit_app/config.py
from __future__ import annotations
import os

# Base URLs to your running services
DATAHUB_BASE_URL = os.getenv("DATAHUB_BASE_URL", "http://localhost:7000")
EVAL_BASE_URL    = os.getenv("EVAL_BASE_URL",    "http://localhost:7001")

# Redis / Garnet (for pub/sub control + telemetry broadcast)
REDIS_URL        = os.getenv("REDIS_URL", "redis://127.0.0.1:6379")

# Polling cadence
REFRESH_FAST_S   = float(os.getenv("REFRESH_FAST_S", "1.5"))   # gauges
REFRESH_SLOW_S   = float(os.getenv("REFRESH_SLOW_S", "5.0"))   # tiers/kpi

# CORS
CORS_ALLOW_ORIGINS = os.getenv("CORS_ALLOW_ORIGINS", "*").split(",")
