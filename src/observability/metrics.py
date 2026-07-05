"""Prometheus metrics for the LVF service — operations tooling, not a wire
protocol concern (no spec/.docx changes; this stays out of the LVF algorithm
spec entirely).

Multiprocess plumbing (PROMETHEUS_MULTIPROC_DIR setup, the /metrics ASGI app,
and the gunicorn child_exit hook) lives in i3_fe_core.observability.metrics —
see that module's docstring for the multiprocess-mode mechanics. This module
holds only LVF's metric definitions.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

from i3_fe_core.observability.metrics import (
    clear_multiproc_dir,  # noqa: F401
    ensure_multiproc_dir,
    mark_worker_dead,  # noqa: F401
    metrics_app,  # noqa: F401
)

# Must happen before the prometheus_client import below — see core module docstring.
ensure_multiproc_dir("/tmp/lvf_prometheus_multiproc")

from prometheus_client import Counter, Histogram

# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

http_requests_total = Counter(
    "lvf_http_requests_total",
    "Total HTTP requests handled, by endpoint and status code.",
    ["endpoint", "status"],
)

http_request_duration_seconds = Histogram(
    "lvf_http_request_duration_seconds",
    "HTTP request handling duration in seconds, by endpoint.",
    ["endpoint"],
)

lost_errors_total = Counter(
    "lvf_lost_errors_total",
    "Total LoST <errors> responses returned from /lost, by error element name.",
    ["error_type"],
)

recursion_total = Counter(
    "lvf_recursion_total",
    "Total outbound LoST recursion attempts, by outcome.",
    ["outcome"],
)

recursion_duration_seconds = Histogram(
    "lvf_recursion_duration_seconds",
    "Outbound LoST recursion call duration in seconds.",
)

reload_events_total = Counter(
    "lvf_reload_events_total",
    "Total GIS data (re)load attempts, by trigger and outcome.",
    ["trigger", "outcome"],
)

load_shed_total = Counter(
    "lvf_load_shed_total",
    "Total /lost requests shed, by reason.",
    ["reason"],
)
