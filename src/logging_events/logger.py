"""Emit structured LVF log events to Python's standard logging."""

from __future__ import annotations

import logging

from src.logging_events.log_events import LostQueryLogEvent, LostResponseLogEvent

log = logging.getLogger(__name__)


def emit_log_event(event: LostQueryLogEvent | LostResponseLogEvent) -> None:
    """Log a structured event. Currently delegates to standard Python logging."""
    log.debug("log_event type=%s data=%r", type(event).__name__, event)
