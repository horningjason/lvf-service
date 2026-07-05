"""Emit structured LVF log events to Python's standard logging and optionally to an i3 Logging Service."""

from __future__ import annotations

import asyncio
import logging

from i3_fe_core.time.timestamps import now_i3
from src import runtime_state
from src.logging.log_events import LostQueryLogEvent, LostResponseLogEvent

log = logging.getLogger(__name__)


def emit_log_event(event: LostQueryLogEvent | LostResponseLogEvent) -> None:
    try:
        asyncio.get_running_loop()
        asyncio.ensure_future(runtime_state.logging_client.emit(event))
    except RuntimeError:
        asyncio.run(runtime_state.logging_client.emit(event))


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
