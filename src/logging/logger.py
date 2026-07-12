"""Emit structured LVF log events to Python's standard logging and optionally to an i3 Logging Service."""

from __future__ import annotations

import asyncio
import logging
import threading

from i3_fe_core.time.timestamps import now_i3
from src import runtime_state
from src.logging.log_events import LostQueryLogEvent, LostResponseLogEvent

log = logging.getLogger(__name__)

# Dedicated, persistent event loop for callers with no running loop of their
# own (e.g. the regression runner's synchronous handle_find_service()).
# runtime_state.logging_client is a single long-lived instance whose httpx
# AsyncClient binds its transport to whichever loop first uses it — calling
# asyncio.run() per emit created and tore down a *new* loop every time,
# orphaning that transport and raising "Event loop is closed" on the very
# next emit. Routing every loop-less emit through one loop that never closes
# keeps the client's transport bound to a single, always-alive loop.
_bg_loop: asyncio.AbstractEventLoop | None = None
_bg_lock = threading.Lock()


def _get_background_loop() -> asyncio.AbstractEventLoop:
    global _bg_loop
    with _bg_lock:
        if _bg_loop is None:
            _bg_loop = asyncio.new_event_loop()
            threading.Thread(target=_bg_loop.run_forever, name="lvf-logging-loop", daemon=True).start()
        return _bg_loop


def emit_log_event(event: LostQueryLogEvent | LostResponseLogEvent) -> None:
    try:
        asyncio.get_running_loop()
        asyncio.ensure_future(runtime_state.logging_client.emit(event))
    except RuntimeError:
        future = asyncio.run_coroutine_threadsafe(
            runtime_state.logging_client.emit(event), _get_background_loop()
        )
        try:
            future.result(timeout=10)
        except Exception:
            log.exception("LogEvent emission failed")


def make_query_event(**kwargs) -> LostQueryLogEvent:
    kwargs.setdefault("timestamp", now_i3())
    return LostQueryLogEvent(
        log_event_type="LostQueryLogEvent",
        **kwargs,
    )


def make_response_event(**kwargs) -> LostResponseLogEvent:
    kwargs.setdefault("timestamp", now_i3())
    return LostResponseLogEvent(
        log_event_type="LostResponseLogEvent",
        **kwargs,
    )
