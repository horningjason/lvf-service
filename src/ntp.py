"""NTP client stub — falls back to system clock."""

from __future__ import annotations

import datetime


class NTPClient:
    """Minimal NTP client. Falls back to datetime.utcnow() until NTP is configured."""

    def get_current_time(self) -> datetime.datetime:
        """Return the current UTC time. Falls back to system clock."""
        return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
