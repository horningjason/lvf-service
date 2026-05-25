"""Emit structured LVF log events to Python's standard logging."""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import os

from dotenv import load_dotenv

from src.logging_events.log_events import LostQueryLogEvent, LostResponseLogEvent

load_dotenv()

log = logging.getLogger(__name__)

_agency_id  = os.getenv("LVF_AGENCY_ID", "")
_server_uri = os.getenv("LVF_SERVER_URI", "lostserver.example.com")

if not _agency_id:
    log.warning(
        "LVF_AGENCY_ID is not set — LostQueryLogEvent and LostResponseLogEvent will have "
        "an empty agencyId field, which is non-conformant per NENA-STA-010.3.1 §4.12.3.1"
    )


def _snake_to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


def _to_camel_dict(d: dict) -> dict:
    return {_snake_to_camel(k): v for k, v in d.items()}


def _serialize(obj):
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def emit_log_event(event: LostQueryLogEvent | LostResponseLogEvent) -> None:
    camel = _to_camel_dict(dataclasses.asdict(event))
    log.info("log_event %s", json.dumps(camel, default=_serialize))


def make_query_event(**kwargs) -> LostQueryLogEvent:
    return LostQueryLogEvent(
        log_event_type="LostQueryLogEvent",
        element_id=_server_uri,
        agency_id=_agency_id,
        **kwargs,
    )


def make_response_event(**kwargs) -> LostResponseLogEvent:
    return LostResponseLogEvent(
        log_event_type="LostResponseLogEvent",
        element_id=_server_uri,
        agency_id=_agency_id,
        **kwargs,
    )
