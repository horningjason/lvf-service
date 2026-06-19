"""Prometheus metrics for the LVF service — operations tooling, not a wire
protocol concern (no spec/.docx changes; this stays out of the LVF algorithm
spec entirely).

Multi-worker correctness: plain prometheus_client does NOT aggregate across
gunicorn workers by default — each worker keeps separate in-memory counters,
so a single scrape would only reflect whichever worker happened to handle it.
This module uses prometheus_client's documented multiprocess mode instead:
metrics are backed by per-process mmap files under PROMETHEUS_MULTIPROC_DIR,
and /metrics is served from a dedicated CollectorRegistry + MultiProcessCollector
that aggregates across every worker's files at scrape time. See
https://prometheus.github.io/client_python/multiprocess/.

PROMETHEUS_MULTIPROC_DIR must be set, and the directory must exist, BEFORE
prometheus_client is first imported anywhere in the process — multiprocess vs
single-process mode is decided once, at prometheus_client.values import time
(prometheus_client/values.py: `ValueClass = get_value_class()`), by checking
os.environ at that moment. Setting the env var later is too late. That is why
the default + directory creation below happen before the `from prometheus_client
import ...` line, and why this module should be the first thing in the process
to import prometheus_client.

Only Counters and Histograms are used here — both aggregate correctly (sum)
across workers in multiprocess mode with no extra configuration. Gauges are
deliberately avoided: they need an explicit multiprocess_mode (e.g. "livesum",
"max") chosen per-metric, which isn't a natural fit for anything tracked here.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

_DEFAULT_MULTIPROC_DIR = "/tmp/lvf_prometheus_multiproc"

# Must happen before the prometheus_client import below — see module docstring.
os.environ.setdefault("PROMETHEUS_MULTIPROC_DIR", _DEFAULT_MULTIPROC_DIR)
os.makedirs(os.environ["PROMETHEUS_MULTIPROC_DIR"], exist_ok=True)

from prometheus_client import CollectorRegistry, Counter, Histogram, multiprocess
from prometheus_client.asgi import make_asgi_app


def clear_multiproc_dir() -> None:
    """Wipe and recreate PROMETHEUS_MULTIPROC_DIR.

    Must be called exactly once, before any worker process starts (per the
    official multiprocess-mode guidance: the directory must be cleared between
    runs). Call this from prewarm.py / main.py / gunicorn's on_starting hook —
    all of which run before workers fork or before the (single) dev process
    starts serving. Never call this from code that runs per-worker: a sibling
    worker's in-flight metric files could be deleted out from under it.
    """
    import shutil

    path = os.environ.get("PROMETHEUS_MULTIPROC_DIR", _DEFAULT_MULTIPROC_DIR)
    if os.path.isdir(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)


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


def mark_worker_dead(pid: int) -> None:
    """Per the official gunicorn child_exit hook pattern — removes a dead
    worker's "live" gauge files so they don't skew livesum/liveall
    aggregation. A no-op for the Counter/Histogram metrics actually defined in
    this module (their per-pid files are cumulative and correctly retained),
    kept here so the standard hook is in place if a Gauge is ever added."""
    multiprocess.mark_process_dead(pid)


def metrics_app():
    """Build the multiprocess-aware ASGI app to mount at GET /metrics.

    Per https://prometheus.github.io/client_python/exporting/http/fastapi-gunicorn/:
    a dedicated CollectorRegistry (NOT the default global registry that
    Counter/Histogram objects above auto-register with) holds only a
    MultiProcessCollector, which reads and aggregates every worker's mmap
    files fresh on each scrape — so this single call below is reused for the
    lifetime of the process; it is not rebuilt per-request.
    """
    registry = CollectorRegistry(support_collectors_without_names=True)
    multiprocess.MultiProcessCollector(registry)
    return make_asgi_app(registry=registry)
