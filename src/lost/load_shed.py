"""Load shedding for POST /lost per spec §3.11.5.

Two independent, operator-configured limits — a per-source sliding-window
rate limit and a per-process global concurrency cap — both disabled (no
limit) by default so existing deployments are unaffected until explicitly
tuned. Both are per-worker, not cross-worker accurate; effective capacity
scales with LVF_WORKERS.

Kept separate from server.py and find_service.py: this is a distinct concern
from routing/validation logic and from the FastAPI request plumbing.

server.py owns the concurrency semaphore's acquire/release lifecycle (via
try_acquire_concurrency() / release_concurrency()) so it can wrap the actual
request handling in try/finally. check() only decides whether a request
should proceed — it does not consume a concurrency slot.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import threading
import time
from typing import Optional

import src.lost.find_service as _fs
from src.observability import metrics as _metrics
from src.notifications import service_state as _service_state
from src.notifications.service_state import ServiceState

log = logging.getLogger(__name__)

PER_SOURCE_LIMIT = "per_source_limit"
CONCURRENCY_CAP = "concurrency_cap"

# ---------------------------------------------------------------------------
# Per-process concurrency gate
# ---------------------------------------------------------------------------


class _NonBlockingSemaphore(asyncio.Semaphore):
    """asyncio.Semaphore with a non-blocking acquire: if no slot is
    immediately available, try_acquire() returns False instead of queuing the
    caller behind other waiters (mirrors the internal counter check that
    Semaphore.acquire() itself performs before deciding to wait)."""

    def try_acquire(self) -> bool:
        if self._value > 0:
            self._value -= 1
            return True
        return False


_semaphore: Optional[_NonBlockingSemaphore] = None
_semaphore_init_lock = threading.Lock()


def _get_semaphore() -> Optional[_NonBlockingSemaphore]:
    """Lazily create the concurrency semaphore sized to
    LVF_MAX_CONCURRENT_REQUESTS. Returns None when disabled (0)."""
    global _semaphore
    limit = _fs._max_concurrent_requests
    if limit <= 0:
        return None
    if _semaphore is None:
        with _semaphore_init_lock:
            if _semaphore is None:
                _semaphore = _NonBlockingSemaphore(limit)
    return _semaphore


def try_acquire_concurrency() -> bool:
    """Non-blocking attempt to take a concurrency slot. Always True when
    LVF_MAX_CONCURRENT_REQUESTS=0 (disabled). Callers that get True back are
    responsible for calling release_concurrency() exactly once when done."""
    sem = _get_semaphore()
    if sem is None:
        return True
    return sem.try_acquire()


def release_concurrency() -> None:
    """Release a slot acquired via try_acquire_concurrency(). No-op when
    concurrency limiting is disabled."""
    sem = _get_semaphore()
    if sem is not None:
        sem.release()


# ---------------------------------------------------------------------------
# Per-source sliding-window rate limit
# ---------------------------------------------------------------------------

_source_lock = threading.Lock()
_source_requests: dict[str, collections.deque] = {}
_check_count = 0
_PRUNE_EVERY = 200          # opportunistic full-table cleanup cadence
_PRUNE_SIZE_THRESHOLD = 1000  # ...or sooner, if the table has grown this large


def _prune_stale_sources(cutoff: float) -> None:
    """Trim stale timestamps across all tracked sources and drop any that end
    up empty. Caller already holds _source_lock. Only invoked occasionally
    (see _check_and_record) so it doesn't add overhead to the common path."""
    empty = []
    for source_ip, dq in _source_requests.items():
        while dq and dq[0] < cutoff:
            dq.popleft()
        if not dq:
            empty.append(source_ip)
    for source_ip in empty:
        del _source_requests[source_ip]


def _check_and_record(source_ip: str) -> bool:
    """Returns True if this request is allowed under the per-source sliding-
    window rate limit; False if it should be rejected. Allowed requests are
    recorded; rejected requests are not. No-op (always True) when
    LVF_RATE_LIMIT_PER_SOURCE=0."""
    global _check_count

    limit = _fs._rate_limit_per_source
    if limit <= 0:
        return True

    window = _fs._rate_limit_window_seconds
    now = time.monotonic()
    cutoff = now - window

    with _source_lock:
        dq = _source_requests.setdefault(source_ip, collections.deque())
        while dq and dq[0] < cutoff:
            dq.popleft()

        if len(dq) >= limit:
            return False

        dq.append(now)

        _check_count += 1
        if _check_count % _PRUNE_EVERY == 0 or len(_source_requests) > _PRUNE_SIZE_THRESHOLD:
            _prune_stale_sources(cutoff)

        return True


# ---------------------------------------------------------------------------
# ServiceState reflection (spec §3.11.6)
#
# Sustained shedding (continuous for _SERVICE_STATE_TRIP_SECONDS) trips
# ServiceState to Partial. It only returns to Normal after shedding has been
# fully quiet for _SERVICE_STATE_CLEAR_SECONDS. These windows are fixed
# constants per spec, not environment-configurable.
# ---------------------------------------------------------------------------

_SERVICE_STATE_TRIP_SECONDS = 15.0
_SERVICE_STATE_CLEAR_SECONDS = 30.0

# A streak is "unbroken" if consecutive sheds are no more than this far apart.
# Tolerates a couple of isolated sheds a second or two apart without resetting
# the streak start, while a genuine multi-second lull still resets it.
_STREAK_GRACE_SECONDS = 2.0

# How often the recovery watcher wakes to check whether shedding has stopped.
_RECOVERY_POLL_INTERVAL_SECONDS = 5.0

_streak_lock = threading.Lock()
_streak_start: Optional[float] = None    # monotonic start of the current unbroken streak; None = not shedding
_last_shed_time: Optional[float] = None  # monotonic time of the most recent shed, of either kind

_recovery_task_started = False
_recovery_task: Optional[asyncio.Task] = None


def _partial_reason(reason: str) -> str:
    if reason == PER_SOURCE_LIMIT:
        return "Sustained load shedding: per-source rate limit"
    return "Sustained load shedding: concurrency cap"


def _record_shed_for_service_state(reason: str) -> None:
    """Update the debounce streak on every shed event and trip ServiceState to
    Partial once the streak has been unbroken for _SERVICE_STATE_TRIP_SECONDS.
    Recovery is NOT decided here — nothing calls this when shedding stops, so
    recovery is handled by the periodic _watch_service_state_recovery() task."""
    global _streak_start, _last_shed_time
    now = time.monotonic()

    with _streak_lock:
        if _streak_start is None or (now - _last_shed_time) > _STREAK_GRACE_SECONDS:
            _streak_start = now
        _last_shed_time = now
        streak_start = _streak_start

    if (
        _service_state.get_state() != ServiceState.Partial
        and (now - streak_start) >= _SERVICE_STATE_TRIP_SECONDS
    ):
        _service_state.set_state(ServiceState.Partial, _partial_reason(reason))


async def _watch_service_state_recovery() -> None:
    """Periodically check whether shedding has been fully quiet for
    _SERVICE_STATE_CLEAR_SECONDS and, if so, return ServiceState to Normal.
    Modeled on find_service.py's mtime-poll watcher pattern, but as an asyncio
    task since this runs alongside other request-handling coroutines rather
    than doing blocking file I/O on a dedicated thread."""
    global _streak_start
    while True:
        await asyncio.sleep(_RECOVERY_POLL_INTERVAL_SECONDS)
        with _streak_lock:
            last = _last_shed_time
        if last is None:
            continue
        if (
            _service_state.get_state() == ServiceState.Partial
            and (time.monotonic() - last) >= _SERVICE_STATE_CLEAR_SECONDS
        ):
            with _streak_lock:
                _streak_start = None
            _service_state.set_state(ServiceState.Normal, "Load shedding stopped")


def start_recovery_watcher_if_needed() -> None:
    """Start the periodic recovery watcher — but only when load shedding is
    actually configured (LVF_RATE_LIMIT_PER_SOURCE or
    LVF_MAX_CONCURRENT_REQUESTS), consistent with this whole feature being a
    no-op by default. Safe to call more than once; only starts the task once.
    Must be called from a running event loop (e.g. application startup)."""
    global _recovery_task_started, _recovery_task
    if _recovery_task_started:
        return
    if _fs._rate_limit_per_source <= 0 and _fs._max_concurrent_requests <= 0:
        return
    _recovery_task_started = True
    _recovery_task = asyncio.create_task(_watch_service_state_recovery(), name="lvf-load-shed-recovery")


async def stop_recovery_watcher() -> None:
    """Cancel the periodic recovery watcher, if running, and await its
    cancellation. Required for graceful shutdown under gunicorn: without this,
    the watcher's open-ended `while True: await asyncio.sleep(...)` keeps the
    event loop alive past graceful_timeout. Resets _recovery_task_started so a
    future startup can start it again cleanly (relevant for tests/dev reload)."""
    global _recovery_task, _recovery_task_started
    if _recovery_task is not None:
        _recovery_task.cancel()
        try:
            await _recovery_task
        except asyncio.CancelledError:
            pass
        _recovery_task = None
    _recovery_task_started = False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def log_shed(reason: str, source_ip: Optional[str] = None) -> None:
    if reason == PER_SOURCE_LIMIT:
        log.warning(
            "Load shed: rejecting /lost request from source=%s — per-source rate limit exceeded",
            source_ip,
        )
    else:
        log.warning("Load shed: rejecting /lost request — global concurrency cap reached")
    _metrics.load_shed_total.labels(reason=reason).inc()
    _record_shed_for_service_state(reason)


def check(source_ip: Optional[str]) -> Optional[str]:
    """Decide whether an inbound /lost request should be shed.

    Runs the per-source rate limit first, then a non-mutating peek at the
    concurrency cap. Returns None if the request should proceed, or a short
    reason string ("per_source_limit" / "concurrency_cap") if it should be
    shed.

    NOTE: this does not acquire a concurrency slot — when this returns None
    and concurrency limiting is enabled, the caller must still call
    try_acquire_concurrency() itself (and release_concurrency() in a
    try/finally) to actually run the request.
    """
    if source_ip is not None and not _check_and_record(source_ip):
        log_shed(PER_SOURCE_LIMIT, source_ip)
        return PER_SOURCE_LIMIT

    sem = _get_semaphore()
    if sem is not None and sem.locked():
        log_shed(CONCURRENCY_CAP, source_ip)
        return CONCURRENCY_CAP

    return None
