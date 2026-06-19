"""Pre-warm the GIS JSON cache before gunicorn forks its workers.

Building the cache once here means the worker processes start against a warm
cache instead of all cold-building the GeoPackage concurrently. No-op in Forest
Guide / routing-only mode.

Run with:
    python prewarm.py
"""

import sys

from dotenv import load_dotenv

load_dotenv()

import src.lost.find_service as find_service


def main() -> int:
    try:
        find_service.prewarm()
    except Exception as exc:  # pragma: no cover - surfaced to the container log
        print(f"Pre-warm failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
