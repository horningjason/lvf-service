"""Graceful shutdown announces GoingDown before the SIP transport closes (§2.4).

Three things have to hold, and each fails silently if it stops holding:

1. ORDER. The GoingDown NOTIFY goes out over the SIP transport that
   sip_notifier.stop() tears down. Announce after the stop and subscribers
   never hear it — they just watch the element vanish. There is no error
   either way, so only an ordering assertion catches a later reshuffle of the
   shutdown block.

2. immediate=True. Core's RFC 6446 rate filter (min_notify_interval=1.0) turns
   a NOTIFY that lands too soon after the previous one into a loop.call_later
   timer, and the loop stops before it fires. A busy element is exactly the one
   that hits this, and exactly the one whose shutdown matters.

3. LEADER GATE. Every worker holds its own StateStore, so an ungated
   announcement fires N times for one element-wide transition.

The notifiers are recorders rather than the real core objects on purpose: what
is under test is LVF's shutdown sequencing, not core's dispatch — core's own
suite covers the rate filter, and tests/test_leader_gate.py covers the gate
core is wired with.
"""

from __future__ import annotations

import asyncio

import pytest

from i3_fe_core.state.store import ElementState, ServiceState

import src.server as server
from src import runtime_state
from src.app import lifecycle as app_lifecycle
from src.federation import coverage as fed_coverage
from src.lost import load_shed


class _RecordingNotifier:
    """Records set_state calls into a shared, ordered event log."""

    def __init__(self, events: list, label: str) -> None:
        self._events = events
        self._label = label

    def set_state(self, state, reason: str = "", immediate: bool = False) -> None:
        self._events.append((self._label, state, reason, immediate))


class _RecordingSipNotifier:
    def __init__(self, events: list) -> None:
        self._events = events

    async def stop(self) -> None:
        self._events.append(("sip-stop",))


class _StubNtpClient:
    """Stands in for NtpClient so the lifespan doesn't reach the network."""

    is_healthy = True
    offset = 0.0

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


@pytest.fixture
def shutdown_events(monkeypatch):
    """Drive _lifespan() with everything but the shutdown announcement stubbed.

    Startup is neutralised (no NTP, no GIS load, no SIP wire, no watchers) so
    the test exercises the shutdown block alone; the returned list is the
    ordered log the assertions read.
    """
    events: list = []

    monkeypatch.setattr(server, "NtpClient", _StubNtpClient)
    monkeypatch.setattr(server, "validate_tls_files", lambda settings: None)
    monkeypatch.setattr(server, "_maybe_start_sip", lambda: None)

    async def _noop_async(*args, **kwargs):
        return None

    monkeypatch.setattr(app_lifecycle, "lifespan_startup", _noop_async)
    monkeypatch.setattr(app_lifecycle, "lifespan_shutdown", _noop_async)
    monkeypatch.setattr(load_shed, "start_recovery_watcher_if_needed", lambda: None)
    monkeypatch.setattr(load_shed, "stop_recovery_watcher", _noop_async)

    monkeypatch.setattr(
        runtime_state, "element_notifier", _RecordingNotifier(events, "element")
    )
    monkeypatch.setattr(
        runtime_state, "service_notifier", _RecordingNotifier(events, "service")
    )
    monkeypatch.setattr(
        server.app.state, "sip_notifier", _RecordingSipNotifier(events), raising=False
    )
    return events


def run_lifespan() -> None:
    """Enter and exit the app lifespan, i.e. a full startup + graceful shutdown."""

    async def _run():
        async with server._lifespan(server.app):
            pass

    asyncio.run(_run())


def test_leader_announces_going_down_before_sip_stops(monkeypatch, shutdown_events):
    monkeypatch.setattr(fed_coverage, "_is_leader", True)

    run_lifespan()

    assert shutdown_events == [
        ("element", ElementState.GOING_DOWN, "graceful shutdown", True),
        ("service", ServiceState.GOING_DOWN, "graceful shutdown", True),
        ("sip-stop",),
    ], (
        "both GoingDown announcements must be dispatched immediately and BEFORE "
        "the SIP transport is torn down"
    )


def test_non_leader_announces_nothing(monkeypatch, shutdown_events):
    """A non-leader worker stays silent — the leader announces for the element."""
    monkeypatch.setattr(fed_coverage, "_is_leader", False)

    run_lifespan()

    assert shutdown_events == [("sip-stop",)]


def test_shutdown_continues_when_the_announcement_raises(monkeypatch, shutdown_events):
    """A notifier blowing up must not strand the rest of the shutdown.

    Everything after this point — the SIP stop, the load-shed watcher, the
    LoST-Sync retry tasks — is what lets the worker exit before gunicorn's
    graceful_timeout, so an exception here has to stay contained.
    """
    monkeypatch.setattr(fed_coverage, "_is_leader", True)

    def _boom(*args, **kwargs):
        raise RuntimeError("notifier exploded")

    monkeypatch.setattr(runtime_state.element_notifier, "set_state", _boom)

    run_lifespan()

    assert ("sip-stop",) in shutdown_events
    # The service half is independent of the element half and still goes out.
    assert ("service", ServiceState.GOING_DOWN, "graceful shutdown", True) in shutdown_events
