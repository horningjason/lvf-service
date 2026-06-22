"""Pre-warm the GIS JSON cache before gunicorn forks its workers.

Building the cache once here means the worker processes start against a warm
cache instead of all cold-building the GeoPackage concurrently. No-op in Forest
Guide / routing-only mode.

Also clears/recreates the Prometheus multiprocess metrics directory — per the
official guidance, that directory must be wiped between runs, and must happen
exactly once, before any worker starts (never per-worker, or a sibling
worker's in-flight metric files could be deleted out from under it).

Run with:
    python prewarm.py
"""

import sys

from dotenv import load_dotenv

load_dotenv()

import src.lost.find_service as find_service
from src.observability import metrics


def main() -> int:
    metrics.clear_multiproc_dir()
    try:
        find_service.prewarm()
    except Exception as exc:  # pragma: no cover - surfaced to the container log
        print(f"Pre-warm failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
