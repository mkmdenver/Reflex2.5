import os
from prometheus_client import Counter, Gauge, Histogram, make_asgi_app

LABELS = dict(
    instance_id=os.getenv("REFLEX__INSTANCE_ID", "default"),
    mode=os.getenv("REFLEX__HUB_MODE", "LIVE"),
)


def with_labels(metric):
    return metric.labels(**LABELS)


# ----------------------------------------------------------------------
# Existing metrics (ws + parquet + basic gauges)
# ----------------------------------------------------------------------

ws_msgs_total = Counter(
    "reflex_ws_msgs_total", "WS messages", ["instance_id", "mode", "type"]
)
ws_reconnects_total = Counter(
    "reflex_ws_reconnects_total", "WS reconnects", ["instance_id", "mode"]
)
ws_drops_total = Counter(
    "reflex_ws_msgs_dropped_total",
    "Dropped WS msgs",
    ["instance_id", "mode", "reason"],
)

parquet_rows_written = Counter(
    "reflex_parquet_rows_written_total",
    "Parquet rows",
    ["instance_id", "mode", "kind"],
)
parquet_rowgroup_flush = Counter(
    "reflex_parquet_rowgroup_flush_total",
    "Rowgroup flushes",
    ["instance_id", "mode", "kind"],
)
parquet_write_seconds = Histogram(
    "reflex_parquet_write_seconds",
    "Write latency",
    ["instance_id", "mode", "kind"],
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 2, 5),
)

hot_symbols = Gauge("reflex_hot_symbols", "HOT symbols", ["instance_id", "mode"])
warm_symbols = Gauge("reflex_warm_symbols", "WARM symbols", ["instance_id", "mode"])
event_loop_lag = Gauge(
    "reflex_event_loop_lag_seconds", "Loop lag", ["instance_id", "mode"]
)


# ----------------------------------------------------------------------
# DataHub-specific hub_* metrics used by datahub.worker
# ----------------------------------------------------------------------

# Total tick/quote events seen at the hub
hub_ticks_total = with_labels(
    Counter(
        "reflex_hub_ticks_total",
        "Total number of tick (trade) events seen by DataHub",
        ["instance_id", "mode"],
    )
)

hub_quotes_total = with_labels(
    Counter(
        "reflex_hub_quotes_total",
        "Total number of quote events seen by DataHub",
        ["instance_id", "mode"],
    )
)

# Last timestamps (seconds since epoch) for tick/quote and refresh
hub_last_tick_ts = with_labels(
    Gauge(
        "reflex_hub_last_tick_ts",
        "Unix timestamp of last tick (trade) seen by DataHub",
        ["instance_id", "mode"],
    )
)

hub_last_quote_ts = with_labels(
    Gauge(
        "reflex_hub_last_quote_ts",
        "Unix timestamp of last quote seen by DataHub",
        ["instance_id", "mode"],
    )
)

hub_last_refresh_ts = with_labels(
    Gauge(
        "reflex_hub_last_refresh_ts",
        "Unix timestamp of last tier/subscribe refresh in DataHub",
        ["instance_id", "mode"],
    )
)

# 1.0 when feed is considered healthy (has active WATCH/WARM/HOT),
# 0.0 when down/stale
hub_feed_ok = with_labels(
    Gauge(
        "reflex_hub_feed_ok",
        "Whether DataHub considers the feed healthy (1.0) or not (0.0)",
        ["instance_id", "mode"],
    )
)


def metrics_asgi_app():
    return make_asgi_app()
