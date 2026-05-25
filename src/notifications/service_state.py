"""Service-state change notifications per NENA-STA-010.3.1 §2.4.2 and §10.12."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from enum import Enum
from typing import Callable, List

log = logging.getLogger(__name__)


class ServiceState(Enum):
    Normal = "Normal"
    Unstaffed = "Unstaffed"
    ScheduledMaintenanceDown = "ScheduledMaintenanceDown"
    ScheduledMaintenanceAvailable = "ScheduledMaintenanceAvailable"
    MajorIncidentInProgress = "MajorIncidentInProgress"
    Partial = "Partial"
    Overloaded = "Overloaded"
    GoingDown = "GoingDown"
    Down = "Down"
    Unreachable = "Unreachable"


class ServiceStateNotifier:
    _MIN_INTERVAL = 1.0  # RFC 6446 minimum notification interval (seconds)

    def __init__(self, service_name: str, service_id: str) -> None:
        self._service_name = service_name
        self._service_id = service_id
        self._state: ServiceState = ServiceState.Normal
        self._reason: str = ""
        self._subscribers: List[Callable] = []
        self._last_notified: float = 0.0
        self._pending_scheduled: bool = False

    def subscribe(self, callback: Callable) -> None:
        self._subscribers.append(callback)

    def get_state(self) -> ServiceState:
        return self._state

    def set_state(self, state: ServiceState, reason: str = "") -> None:
        if state == self._state and reason == self._reason:
            return

        self._state = state
        self._reason = reason
        log.info("ServiceState changed to %s: %s", state.value, reason)

        now = time.monotonic()
        elapsed = now - self._last_notified

        if elapsed >= self._MIN_INTERVAL:
            self._last_notified = now
            self._fire_subscribers()
        else:
            if not self._pending_scheduled:
                self._pending_scheduled = True
                delay = self._MIN_INTERVAL - elapsed
                try:
                    loop = asyncio.get_running_loop()
                    loop.call_later(delay, self._fire_pending)
                except RuntimeError:
                    pass

    def _fire_pending(self) -> None:
        self._pending_scheduled = False
        self._last_notified = time.monotonic()
        self._fire_subscribers()

    def _fire_subscribers(self) -> None:
        body = self.get_notify_body()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for cb in self._subscribers:
            try:
                result = cb(body)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result, loop=loop)
            except Exception as exc:
                log.warning("ServiceState subscriber raised: %s", exc)

    def get_notify_body(self) -> dict:
        return {
            "service": self._service_name,
            "name": self._service_name,
            "serviceId": self._service_id,
            "serviceState": {
                "state": self._state.value,
                "reason": self._reason,
            },
        }


_notifier = ServiceStateNotifier(
    service_name="LVF",
    service_id=os.environ.get("LVF_SERVER_URI", "lostserver.example.com"),
)


def set_state(state: ServiceState, reason: str = "") -> None:
    _notifier.set_state(state, reason)


def get_state() -> ServiceState:
    return _notifier.get_state()


def get_notify_body() -> dict:
    return _notifier.get_notify_body()


def subscribe(callback: Callable) -> None:
    _notifier.subscribe(callback)
