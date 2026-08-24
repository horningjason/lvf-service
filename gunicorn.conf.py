"""Gunicorn configuration for the LVF service (per-machine multi-worker).

Run with:
    python prewarm.py && gunicorn -c gunicorn.conf.py src.server:app

Worker count is controlled by LVF_WORKERS (default 1 == today's single-process
behavior).

TLS: file-existence validation mirrors main.py, but the actual SSL context is
NOT built here through gunicorn's own certfile/keyfile/cert_reqs/ca_certs
settings — that path cannot express i3 §2.8.1's TLS-1.2 floor or PFS cipher
suite, and is the exact mechanism this repo's own commit b36d4f8 ("mTLS
gunicorn") tried and d22c312 ("Reverting mTLS work") reverted six minutes
later (see src/app/lvf_uvicorn_worker.py's docstring for the full history).
worker_class below points at that module's LvfUvicornWorker, which builds the
context through i3_fe_core.security.tls.make_server_ssl_context() and injects
it directly — see that module's docstring for the full mechanism and why
certfile/keyfile/cert_reqs/ca_certs are deliberately left unset here.

Multi-worker requires Linux or Docker — gunicorn is POSIX-only.
"""

import os
import sys

from src.core_components import build_tls_settings, validate_tls_files
from src.observability import metrics

bind = "0.0.0.0:8000"
worker_class = "src.app.lvf_uvicorn_worker.LvfUvicornWorker"
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


# -- TLS file-existence validation (mirrors main.py) --------------------------
# Fails fast in the gunicorn master before any worker forks. The actual TLS
# context is built per-worker by LvfUvicornWorker (see module docstring
# above) — this block only validates the files it will read exist.
_tls_error = validate_tls_files(build_tls_settings())
if _tls_error is not None:
    print(f"ERROR: {_tls_error}", file=sys.stderr)
    sys.exit(1)
