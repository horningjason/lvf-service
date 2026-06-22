"""Gunicorn configuration for the LVF service (per-machine multi-worker).

Run with:
    gunicorn -c gunicorn.conf.py src.server:app

Worker count is controlled by LVF_WORKERS (default 1 == today's single-process
behavior). TLS handling mirrors main.py exactly so HTTPS/mTLS work identically
whether you launch via `python main.py` (dev) or gunicorn (production).

The Forest Guide must run single-worker (LVF_WORKERS=1).
"""

import os
import ssl

from src.observability import metrics

bind = "0.0.0.0:8000"
worker_class = "uvicorn.workers.UvicornWorker"
workers = int(os.environ.get("LVF_WORKERS", "1"))          # default 1 == today's behavior
timeout = int(os.environ.get("LVF_WORKER_TIMEOUT", "120"))
graceful_timeout = 30


def on_starting(server):
    """Runs once in the gunicorn master, before any worker forks — the only
    safe place to clear the Prometheus multiprocess directory (clearing it
    per-worker would race with siblings writing to it). Normally prewarm.py
    already did this; this is a defense-in-depth, harmless-if-redundant
    re-run for anyone invoking gunicorn directly without prewarm.py first."""
    metrics.clear_multiproc_dir()


def child_exit(server, worker):
    """Official prometheus_client multiprocess pattern: remove a dead
    worker's metric files on exit. See src/observability/metrics.py:mark_worker_dead."""
    metrics.mark_worker_dead(worker.pid)

# ── TLS (mirrors main.py) ─────────────────────────────────────────────────────
_tls_mode = os.environ.get("LVF_TLS_MODE", "disabled").lower()
if _tls_mode in ("tls", "mtls"):
    _cert = os.environ.get("LVF_TLS_CERT_FILE", "")
    _key = os.environ.get("LVF_TLS_KEY_FILE", "")

    if not _cert or not os.path.exists(_cert):
        raise RuntimeError(
            f"LVF_TLS_CERT_FILE must be set and the file must exist (got: {_cert!r})"
        )
    if not _key or not os.path.exists(_key):
        raise RuntimeError(
            f"LVF_TLS_KEY_FILE must be set and the file must exist (got: {_key!r})"
        )

    certfile = _cert
    keyfile = _key

    if _tls_mode == "mtls":
        _ca = os.environ.get("LVF_TLS_CA_FILE", "")
        if not _ca or not os.path.exists(_ca):
            raise RuntimeError(
                f"LVF_TLS_CA_FILE must be set and the file must exist for mtls mode "
                f"(got: {_ca!r})"
            )
        ca_certs = _ca
        # CERT_OPTIONAL: requests a client certificate but does not require
        # one — connections without a client cert are still accepted. Known
        # limitation: this is not equivalent to enforcing mTLS.
        cert_reqs = ssl.CERT_OPTIONAL
