"""Structured log event types for LVF query and response logging."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from i3_fe_core.logging.logevent import LogEventPrologue


def generate_query_id() -> str:
    """Return a globally unique LoST query ID per NENA-STA-010.3f-2021 §4.12.3.7."""
    return f"urn:emergency:uid:queryid:{uuid.uuid4()}"


@dataclass
class LostQueryLogEvent(LogEventPrologue):
    # LostQueryLogEvent-specific fields — §4.12.3.7
    query_id:         str = ""     # urn:emergency:uid:queryid:<uuid4>
    direction:        str = ""     # "incoming" or "outgoing"
    query_adapter:    str = ""     # entire LoST request XML as string
    malformed_query:  Optional[str] = None  # raw request if malformed, truncated to 2048 chars
    # service_id is a subclass field, not a prologue field — §4.12.3.1 excludes
    # serviceId from the common prologue. OPTIONAL per i3; will be mandatory in future.
    service_id:       Optional[str] = None


@dataclass
class LostResponseLogEvent(LogEventPrologue):
    # LostResponseLogEvent-specific fields — §4.12.3.7
    response_id:        str = ""     # MUST match query_id of paired LostQueryLogEvent
    direction:          str = ""     # "incoming" or "outgoing"
    response_adapter:   str = ""     # entire LoST response XML as string
    response_status:    Optional[str] = None  # status code if malformed/error
    malformed_response: Optional[str] = None
    service_id:          Optional[str] = None
