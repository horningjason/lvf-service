"""Element-state change notifications per NENA-STA-010.3.1 §2.4.1 and §10.13."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from enum import Enum
from typing import Callable, List, Optional

log = logging.getLogger(__name__)


class ElementState(Enum):
    Normal = "Normal"
    ScheduledMaintenance = "ScheduledMaintenance"
    ServiceDisruption = "ServiceDisruption"
    Overloaded = "Overloaded"
    GoingDown = "GoingDown"
    Down = "Down"
    Unreachable = "Unreachable"


class ElementStateNotifier:
    _MIN_INTERVAL = 1.0  # RFC 6446 minimum notification interval (seconds)

    def __init__(self) -> None:
        self._state: ElementState = ElementState.Normal
        self._reason: str = ""
        self._subscribers: List[Callable] = []
        self._last_notified: float = 0.0
        self._pending_scheduled: bool = False

    def subscribe(self, callback: Callable) -> None:
        self._subscribers.append(callback)

    def get_state(self) -> ElementState:
        return self._state

    def set_state(self, state: ElementState, reason: str = "") -> None:
        if state == self._state and reason == self._reason:
            return

        self._state = state
        self._reason = reason
        log.info("ElementState changed to %s: %s", state.value, reason)

        now = time.monotonic()
        elapsed = now - self._last_notified

        if elapsed >= self._MIN_INTERVAL:
            self._last_notified = now
            self._fire_subscribers()
        else:
            # Rate-limited: schedule one deferred notification (latest state wins)
            if not self._pending_scheduled:
                self._pending_scheduled = True
                delay = self._MIN_INTERVAL - elapsed
                try:
                    loop = asyncio.get_running_loop()
                    loop.call_later(delay, self._fire_pending)
                except RuntimeError:
                    pass  # No running loop — deferred fire not possible

    def _fire_pending(self) -> None:
        self._pending_scheduled = False
        self._last_notified = time.monotonic()
        self._fire_subscribers()

    def _fire_subscribers(self) -> None:
        body = self.get_notify_body()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # Not in async context — skip subscriber callbacks
        for cb in self._subscribers:
            try:
                result = cb(body)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result, loop=loop)
            except Exception as exc:
                log.warning("ElementState subscriber raised: %s", exc)

    def get_notify_body(self) -> dict:
        return {
            "elementId": os.environ.get("LVF_SERVER_URI", "lostserver.example.com"),
            "state": self._state.value,
            "reason": self._reason,
        }


_notifier = ElementStateNotifier()


def notify_degraded(component: str) -> None:
    """Sets state to ServiceDisruption with reason "Component degraded: <component>"."""
    _notifier.set_state(
        ElementState.ServiceDisruption,
        reason=f"Component degraded: {component}",
    )


def notify_restored() -> None:
    """Sets state to Normal with reason "Component restored"."""
    _notifier.set_state(ElementState.Normal, reason="Component restored")


def get_state() -> ElementState:
    return _notifier.get_state()


def get_notify_body() -> dict:
    return _notifier.get_notify_body()


def subscribe(callback: Callable) -> None:
    _notifier.subscribe(callback)
