"""Structured log event types for LVF query and response logging."""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Optional


def generate_query_id() -> str:
    """Return a globally unique LoST query ID per NENA-STA-010.3.1 §4.12.3.7."""
    return f"urn:emergency:uid:queryid:{uuid.uuid4()}"


@dataclass
class LostQueryLogEvent:
    # Common LogEvent prologue — §4.12.3.1
    log_event_type:   str                # always "LostQueryLogEvent"
    timestamp:        datetime.datetime  # UTC datetime with timezone
    element_id:       str                # populated from LVF_SERVER_URI
    agency_id:        str                # populated from LVF_AGENCY_ID
    call_id:          Optional[str] = None  # from emergencyCallIncidentId extension
    incident_id:      Optional[str] = None  # from emergencyCallIncidentId extension
    ip_address_port:  Optional[str] = None  # remote client address if known
    service_id:       Optional[str] = None  # OPTIONAL per i3; will be mandatory in future

    # LostQueryLogEvent-specific fields — §4.12.3.7
    query_id:         str = ""     # urn:emergency:uid:queryid:<uuid4>
    direction:        str = ""     # "incoming" or "outgoing"
    query_adapter:    str = ""     # entire LoST request XML as string
    malformed_query:  Optional[str] = None  # raw request if malformed, truncated to 2048 chars


@dataclass
class LostResponseLogEvent:
    # Common LogEvent prologue — §4.12.3.1
    log_event_type:     str                # always "LostResponseLogEvent"
    timestamp:          datetime.datetime  # UTC datetime with timezone
    element_id:         str                # populated from LVF_SERVER_URI
    agency_id:          str                # populated from LVF_AGENCY_ID
    call_id:            Optional[str] = None
    incident_id:        Optional[str] = None
    ip_address_port:    Optional[str] = None
    service_id:         Optional[str] = None

    # LostResponseLogEvent-specific fields — §4.12.3.7
    response_id:        str = ""     # MUST match query_id of paired LostQueryLogEvent
    direction:          str = ""     # "incoming" or "outgoing"
    response_adapter:   str = ""     # entire LoST response XML as string
    response_status:    Optional[str] = None  # status code if malformed/error
    malformed_response: Optional[str] = None
