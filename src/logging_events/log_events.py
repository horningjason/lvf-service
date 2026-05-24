"""Structured log event types for LVF query and response logging."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LostQueryLogEvent:
    """Structured log event for an inbound LoST findService request."""
    service_urn:       str
    country:           Optional[str] = None
    a1:                Optional[str] = None
    a2:                Optional[str] = None
    a3:                Optional[str] = None
    validate_location: str = "false"
    recursive:         bool = False


@dataclass
class LostResponseLogEvent:
    """Structured log event for an outbound LoST findService response."""
    outcome:    str                    # e.g. "locationValidation", "notFound", "redirect"
    valid:      list[str] = field(default_factory=list)
    invalid:    Optional[str] = None
    unchecked:  list[str] = field(default_factory=list)
    layer:      Optional[str] = None   # "SSAP", "RCL", or None
    duration_ms: Optional[float] = None
