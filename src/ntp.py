"""NTP client — synchronizes server time via ntplib with system clock fallback."""

from __future__ import annotations

import datetime
import logging
import os
from typing import Optional

import ntplib

log = logging.getLogger(__name__)


class NTPClient:
    """
    NTP client that falls back to system clock on query failure.

    Reads configuration from the environment at instantiation:
      LVF_NTP_SERVER  — hostname of the NTP server (default: pool.ntp.org)
      LVF_NTP_VERSION — NTP protocol version as int (default: 3)
      LVF_NTP_TIMEOUT — query timeout in seconds as float (default: 5.0)
    """

    def __init__(self) -> None:
        # None means "not configured" — get_current_time() returns system clock immediately
        self._server: Optional[str] = os.environ.get("LVF_NTP_SERVER") or None
        try:
            self._version: int = int(os.environ.get("LVF_NTP_VERSION", "3"))
        except (ValueError, TypeError):
            self._version = 3
        try:
            self._timeout: float = float(os.environ.get("LVF_NTP_TIMEOUT", "5.0"))
        except (ValueError, TypeError):
            self._timeout = 5.0
        self._last_sync_time: Optional[datetime.datetime] = None
        self._is_synchronized: bool = False

    @property
    def server(self) -> Optional[str]:
        """Configured NTP server hostname, or None if NTP is not configured."""
        return self._server

    @property
    def version(self) -> int:
        """Configured NTP protocol version."""
        return self._version

    @property
    def timeout(self) -> float:
        """Configured NTP query timeout in seconds."""
        return self._timeout

    @property
    def last_sync_time(self) -> Optional[datetime.datetime]:
        """UTC time of the last successful NTP sync, or None if never synced."""
        return self._last_sync_time

    @property
    def is_synchronized(self) -> bool:
        """True if the last get_current_time() call succeeded via NTP."""
        return self._is_synchronized

    def get_current_time(self) -> datetime.datetime:
        """
        Return the current UTC time.

        On success: returns the NTP-derived UTC datetime and sets is_synchronized=True.
        On any failure: logs a WARNING, returns datetime.now(timezone.utc), and sets
        is_synchronized=False.  If this call is the first failure after a run of
        successes, also calls notify_degraded("ntp") from element_state if available.

        If LVF_NTP_SERVER is not set, returns system clock immediately without any
        NTP attempt or log output.
        """
        if self._server is None:
            return datetime.datetime.now(datetime.timezone.utc)

        try:
            client = ntplib.NTPClient()
            resp = client.request(self._server, version=self._version, timeout=self._timeout)
            t = datetime.datetime.fromtimestamp(resp.tx_time, tz=datetime.timezone.utc)
            self._last_sync_time = t
            self._is_synchronized = True
            return t
        except Exception as exc:
            log.warning(
                "NTP query to %s failed: %s — falling back to system clock",
                self._server, exc,
            )
            if self._is_synchronized:
                # Transition: synchronized → degraded
                try:
                    from src.notifications.element_state import notify_degraded
                    notify_degraded("ntp")
                except (ImportError, AttributeError):
                    pass
            self._is_synchronized = False
            return datetime.datetime.now(datetime.timezone.utc)
