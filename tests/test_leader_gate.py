"""Leader gate on §4.12.3 state-change LogEvents (multi-worker).

WHY THIS TEST LIVES HERE AND NOT IN i3-fe-core

i3-fe-core owns the *gate* — ElementStateNotifier/ServiceStateNotifier take an
`is_leader` callable and skip the LogEvent while it returns False — and its own
suite covers that with a stub predicate.  What core cannot cover is the half
this repo owns: that LVF actually *passes* a predicate, and that the predicate
is backed by the real cross-process election in src/federation/coverage.py
rather than by something that merely happens to be True in a test.  Those are
the two ways the gate silently regresses, and both are invisible from core's
side.

So these tests drive the election for real: a second process takes the same
flock() the leader lock uses, and this process then loses
`_acquire_leadership()` the way a non-leader gunicorn worker does.  Nothing
about leadership is mocked.

WHAT IS AND IS NOT GATED.  The gate covers the LogEvent (and therefore the POST
to LVF_LOGGING_SERVICE_URI) only.  Core still fans the NOTIFY body out to every
local subscriber in every worker — the SIP adapter depends on that — so
test_non_leader_still_fans_out_notify pins the distinction.  A "fix" that
suppressed the callbacks too would pass the suppression test and break SIP.

PLATFORM.  The election is flock()-based, so only POSIX can produce a genuine
non-leader: on Windows `coverage._fcntl is None` and every process reports
itself leader by design.  The contention tests skip there — a skip is not a
pass.  The wiring and always-emit baselines run everywhere.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from i3_fe_core.state.store import ElementState, ServiceState

from src.federation import coverage as fed_coverage

requires_flock = pytest.mark.skipif(
    fed_coverage._fcntl is None,
    reason="leader election is flock()-based — POSIX only (see coverage._acquire_leadership)",
)


class _RecordingLoggingClient:
    """Stands in for LoggingClient at the one call site the gate guards.

    The LOGGING side is stubbed on purpose: what is under test is whether the
    emission happens at all, not what core builds or how it POSTs it.
    """

    def __init__(self) -> None:
        self.events: list[object] = []

    def emit_nowait(self, event):  # mirrors LoggingClient.emit_nowait
        self.events.append(event)
        return {}


# Holds an exclusive flock on the leader lock path, then blocks, so the test
# process contends for leadership against a real lock held by a real other
# process — the same shape as a second gunicorn worker.
_LOCK_HOLDER_SRC = textwrap.dedent(
    """
    import fcntl, os, sys, time
    fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    sys.stdout.write("locked\\n")
    sys.stdout.flush()
    time.sleep(120)
    """
)


@pytest.fixture
def leader_state(tmp_path, monkeypatch):
    """Isolate the leader lock in tmp_path and restore module state afterwards."""
    # _leader_lock_path() derives the lock from the GPKG's directory.
    monkeypatch.setenv("LVF_GPKG_PATH", str(tmp_path / "lvf.gpkg"))
    monkeypatch.setenv("LVF_TLS_MODE", "disabled")
    monkeypatch.delenv("LVF_LOGGING_SERVICE_URI", raising=False)

    prev_leader = fed_coverage._is_leader
    prev_fd = fed_coverage._leader_lock_fd
    fed_coverage._leader_lock_fd = None
    try:
        yield
    finally:
        fd = fed_coverage._leader_lock_fd
        if fd is not None and fd != prev_fd:
            try:
                os.close(fd)
            except OSError:
                pass
        fed_coverage._is_leader = prev_leader
        fed_coverage._leader_lock_fd = prev_fd


@pytest.fixture
def wired():
    """The real build_core_components() wiring, with the logging client spied.

    Built through the production factory rather than by constructing notifiers
    directly: the unwired-gate regression this guards against is precisely a
    missing `is_leader=` argument in that factory.
    """
    from src.core_components import build_core_components

    components = build_core_components()
    recorder = _RecordingLoggingClient()
    components.element_notifier._logging_client = recorder
    components.service_notifier._logging_client = recorder
    return components, recorder


@pytest.fixture
def lock_holder(leader_state):
    """A separate process holding the leader lock for the duration of a test."""
    lock_path = fed_coverage._leader_lock_path()
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER_SRC, lock_path],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        ready = proc.stdout.readline()
        assert ready.strip() == "locked", "lock holder failed to take the lock"
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_notifiers_are_wired_with_a_leader_gate(leader_state, wired):
    """The gate exists at all — `is_leader=` was actually passed.

    Without this, an unwired notifier (is_leader=None, always emit) would still
    pass the leader case below, and only the POSIX-only contention test would
    catch the regression — i.e. nothing would catch it on Windows.
    """
    components, _ = wired
    assert components.element_notifier._is_leader is not None
    assert components.service_notifier._is_leader is not None
    # And it reads the election live, rather than having captured a bool at
    # construction time — leadership is decided later, in lifespan_startup().
    fed_coverage._is_leader = False
    assert components.element_notifier._is_leader() is False
    assert components.service_notifier._is_leader() is False
    fed_coverage._is_leader = True
    assert components.element_notifier._is_leader() is True
    assert components.service_notifier._is_leader() is True


def test_leader_emits_state_log_events(leader_state, wired):
    """Baseline: uncontended, this process wins the election and both events go out."""
    components, recorder = wired

    assert fed_coverage._acquire_leadership() is True
    assert fed_coverage._is_leader is True

    components.element_notifier.set_state(ElementState.GOING_DOWN, "graceful shutdown")
    components.service_notifier.set_state(ServiceState.GOING_DOWN, "graceful shutdown")

    assert len(recorder.events) == 2, "leader must emit both state-change LogEvents"


@requires_flock
def test_non_leader_suppresses_state_log_events(wired, lock_holder):
    """A worker that loses the real file-lock election emits neither LogEvent."""
    components, recorder = wired

    assert fed_coverage._acquire_leadership() is False, (
        "another process holds the leader lock — this process must not be leader"
    )
    assert fed_coverage._is_leader is False

    components.element_notifier.set_state(ElementState.GOING_DOWN, "graceful shutdown")
    components.service_notifier.set_state(ServiceState.GOING_DOWN, "graceful shutdown")

    assert recorder.events == [], (
        "non-leader worker must not emit state-change LogEvents — the leader "
        "already logs the same transition"
    )


@requires_flock
def test_non_leader_still_fans_out_notify(wired, lock_holder):
    """The gate covers the LogEvent only — local NOTIFY subscribers still fire.

    Every worker runs its own StateStore and its own subscribers; the SIP wire
    adapter is one of them. Suppressing the callbacks alongside the LogEvent
    would silence state notifications on non-leader workers.
    """
    components, recorder = wired
    element_bodies: list[dict] = []
    service_bodies: list[dict] = []
    components.element_notifier.subscribe(element_bodies.append)
    components.service_notifier.subscribe(service_bodies.append)

    assert fed_coverage._acquire_leadership() is False

    components.element_notifier.set_state(ElementState.GOING_DOWN, "graceful shutdown")
    components.service_notifier.set_state(ServiceState.GOING_DOWN, "graceful shutdown")

    assert recorder.events == []
    assert len(element_bodies) == 1
    assert len(service_bodies) == 1
